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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
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
#:
#: **The comparison is `>`, and that is deliberate** (re-examined 2026-08-26, after an audit
#: reported it as a blind spot: a shortfall of exactly one frame is `1/24`, which is not
#: `> 1/24`, so a single clip losing its last frame could ship unflagged). It stays `>` for
#: four reasons, the last of them measured:
#:
#: * the rule this bound *is* — stated here, in `EXPORT_DURATION_PROBLEM`'s own sentence ("off
#:   by more than one frame"), in `AGENTS.md` and in `docs/ROADMAP.md` — is **within** one
#:   frame. A discrepancy of exactly one frame is within one frame;
#: * `test_the_boundary_tolerance_is_half_a_frame_and_coverage_is_one_frame` already pins that
#:   reading on the plan half: a plan short of the song by exactly `one_frame` is accepted;
#: * `web/assets/api.js` mirrors this constant and its `>` for the timeline's head/tail
#:   warnings, held equal by a contract test, so the operator is not this module's alone;
#: * and it would change nothing. Measured 2026-08-26: over 100 000 random song lengths from
#:   30 s to 300 s, a one-frame-short export flags 49 968 times under `>` and **the same 49 968
#:   times** under `>=` — not one case differs. `export_seconds` arrives from ffprobe as six
#:   decimals, so a one-frame difference reaches this comparison already rounded a few tenths
#:   of a microsecond either side of `1/24`; which side is noise, and it decides the answer
#:   where the operator does not. Real exports one and two frames short, built and probed the
#:   way the assemble route builds and probes them, were both flagged as they stand.
#:
#: **The honest limit, recorded rather than fixed.** This is a whole-file duration check, so it
#: cannot distinguish a rounding artefact from a real per-clip frame loss, and at one frame it
#: cannot reliably see either. Two frames it always sees. The thing that actually guarantees
#: the frame rule per clip is `effects.BRANCH_FRAME_GUARD` and the `-frames:v` cap in
#: `trim_args`; this bound is the backstop, not the guarantee, and tightening the operator
#: would not make it one.
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
#: R-37, and the one refusal in this module that refuses **something other than the export**.
#:
#: A Transition is a blend of exactly two pictures. Where a third clip covers any part of the
#: Overlap the two shots make, there is no pair of legs to blend: `assembly_plan` already resolves
#: that geometry into more than two visible ranges, and story 9.7 shipped composing them apart on
#: purpose. Refusing the *export* over it would be **stricter than `assembly_plan` itself** and
#: would cost a Director a render over one geometry, so the boundary stays the hard cut it is
#: today and this sentence is recorded on `ExportLook.transitions` instead — which is the only
#: place that can say a transition the manifest holds did not run.
#:
#: The wording is `timeline.SNAP_NESTED`'s argument in this module's vocabulary, and it is
#: deliberately **not** that constant reused: `SNAP_NESTED` states that one window *sits entirely
#: inside* another, which is one of the shapes reaching here and not the common one -- three
#: consecutive shots each dragged over the next until the two overlaps touch is the ordinary way
#: to reach this, and no window is nested in it. A refusal that stated a falsehood about the
#: geometry would be worse than a second sentence. The remedy is `SNAP_NESTED`'s own, because it
#: is the same remedy: move one of them out from under the others.
#: The other geometry a transition has no legs for: one of the two Shots laid **wholly inside** the
#: other. Nested either way there is a leg with nothing behind it — the incoming Shot swallowed
#: leaves no head after the blend, the outgoing Shot swallowed leaves no tail before it — which is
#: the defect the first run of `test_a_shot_swallowed_whole_...` produced (`frames=[48, 144, -96,
#: 96]`, a frame count below zero reaching the grid sum).
#:
#: **This sentence no longer decides anything, and that is the correction of 2026-08-30.** Nesting
#: was a *condition* here, tested against `after.end` while the split's own third entry was cut
#: from `head.end`, and two answers to one question in adjacent lines is how the same defect went
#: on shipping in two shapes this branch could not see. The decision is now `_split_frames`' —
#: one measurement of what the split would lay — and this constant is what that measurement is
#: *called* when the emptiness it found is a nested window. Both directions are named by it: the
#: format's `{before}` is whichever Shot is on the outside, which is not always the earlier one.
#:
#: **This is as close to reusing `timeline.SNAP_NESTED` as this module can get, and the distance
#: is worth stating.** R-37 asks for that constant's wording, and `assembly.py` **may not import
#: it**: it is one of AD-25's leaf modules and `tests/test_module_boundaries.py` enforces
#: "the standard library and nothing else from this package" as a guard rather than a discipline.
#: So the first sentence and the remedy are `SNAP_NESTED`'s own, character for character, and
#: `test_the_nested_refusal_says_what_the_snapper_says` holds the two together across the boundary
#: the import cannot cross. What is *not* borrowed is the middle: the snapper's *"there is no
#: single point here to place"* is about placing a cut, and a Director who asked for a blend and
#: was told about cut placement would be reading a true remedy with a false reason attached.
TRANSITION_NESTED_REFUSAL = (
    "{after} sits entirely inside {before} — {before} runs {start:.3f}s to {end:.3f}s and "
    "{after}'s {inner_start:.3f}s to {inner_end:.3f}s is within it. A transition blends the tail "
    "of one shot into the head of the next, and a clip laid wholly over another leaves neither, "
    "so this boundary stays a hard cut. "
    "Move one of them out from under the other."
)
TRANSITION_CROWDED_REFUSAL = (
    "{before} and {after} overlap from {start:.3f}s to {end:.3f}s, but {count} clips cover part "
    "of that stretch. A transition blends exactly two pictures, so there is no single pair here "
    "to blend and this boundary stays a hard cut. Move the others out from under it, or shorten "
    "the overlap so only these two share it."
)
#: What a degenerate split is called when neither of the two sentences above describes it, and it
#: is the one that says the measurement out loud: the three stretches the boundary would become,
#: in the order they play, in the frames the export would write for each.
#:
#: **Two geometries reach this and no enumeration of geometries found either** (2026-08-30). A
#: third clip starting inside the half-frame band below the Overlap's end is excluded from the
#: crowding count — the tolerance is applied inwards, deliberately, because a boundary written
#: twice is one boundary — while still truncating the incoming Shot's head, so the third entry
#: runs backwards: `A[0,4.0625] B[3,6] C[4.05,8]` gave `[72, 26, -1, 95]`, summing to the song
#: exactly, refusing nothing, and shipping half the running time as the wrong Shot at HTTP 200
#: because `-frames:v -1` is *ignored* by ffmpeg at rc 0 (measured: asked −1, wrote the whole rest
#: of the take). Its sibling `A[0,4] B[3,6] C[4,8]` needs no off-grid arithmetic at all and is the
#: snapper's preferred outcome — a clip whose start snaps to the previous clip's end — and gives
#: `[72, 24, 0, 96]`.
#:
#: The sentence therefore states **all three numbers** rather than the empty one, because which of
#: them is empty is the finding and a Director who can see 26 frames of blend between 72 and −1 can
#: see which end of their timeline to go and look at. It names the boundary by both Shots, which
#: `routes/shots.render_boundary_preview` also relies on: that route picks this plan's sentence out
#: by the outgoing Shot's label.
TRANSITION_EMPTY_SPLIT_REFUSAL = (
    "{before} and {after} overlap from {start:.3f}s to {end:.3f}s, and on the assembly grid that "
    "boundary is {outgoing} frames of {before}, a {blend}-frame blend, then {incoming} frames of "
    "{after}. A transition is a stretch of frames with one shot before it and the other after it, "
    "so a boundary where any of the three is empty stays a hard cut. Shorten the overlap, or move "
    "whatever else covers this stretch out from under it."
)
#: The guard on `assembly_plan`'s own output, and it is meant to be unreachable. See
#: `assembly_plan`'s docstring for why a negative count is raised where a zero is dropped.
ASSEMBLY_NEGATIVE_FRAMES_ERROR = (
    "assembly_plan produced {frames} for {label} ({start:.3f}s to {end:.3f}s), which is not a "
    "number of frames anything can render. The frame grid is only exact while every entry lays "
    "frames forwards."
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


@dataclass(frozen=True, slots=True)
class TransitionChoice:
    """A Transition, as `assembly_plan` needs it: an id to record and an `xfade` name to run.

    **Resolved by the caller, never by this module.** `effects.TRANSITION_CATALOGUE` owns which
    transitions exist and which `xfade` each is, exactly as it owns which effects exist -- and
    this module goes on importing nothing from it, the way `trim_args` receives two lists of
    finished stage strings rather than a stack. The route looks the type up and hands over the
    two strings; the grid arithmetic below cannot be reached by a catalogue at all.
    """

    transition_id: str
    xfade: str


@dataclass(slots=True)
class TransitionClip:
    """One Overlap resolved as a blend: both legs, already windowed to the Overlap, and the
    `xfade` that joins them.

    **It rides in `plan.clips`, as a union entry, and not in a sibling list** (R-39). The frame
    grid's whole guarantee is that `sum(plan.frames)` telescopes over *one* ordered list of
    boundaries; two lists would let the audio-overlay accumulator and the grid sum be computed
    from sources that can disagree, and FX-NFR-1 is the one thing this epic may not get wrong. The
    cost -- every `zip(plan.clips, plan.frames)` site has to decide what a transition entry means
    to it -- is paid deliberately, and it is paid in the visible currency: this is a different
    type, so a site that has not decided does not compile rather than doing the wrong thing
    quietly. That is the whole reason it is not a flag on `ClipWindow`.

    `before` is the earlier Shot's **tail** and `after` the later Shot's **head**, both already
    `replace`d onto the Overlap's own window with their take offsets advanced by exactly the
    seconds skipped -- the same arithmetic `assembly_plan`'s resolution loop has always used to
    split a clip. So each leg is an ordinary `ClipWindow` that happens to be one of a pair, and
    everything that reads a clip (its source, its offset, whether its take audio is accepted)
    reads it unchanged.

    **The frames are provably available and no over-render margin is borrowed** (AD-19, R-38).
    `app._window_refusals` runs `assembly_refusals` over the **full Shot windows**, before any
    overlap is resolved, so `take_cut_refusal` has already proved each take holds its whole
    window -- and the Overlap is inside both windows by construction. Re-cutting the blend from
    the finished intermediates is not merely costlier, it is impossible: the earlier clip is
    truncated at the later one's start, so `before`'s frames are in no intermediate at all.
    """

    before: ClipWindow
    after: ClipWindow
    choice: TransitionChoice

    @property
    def start(self) -> float:
        """The Overlap's start, which is the later Shot's own window start.

        Read off `after` rather than stored, so this entry cannot come to a second opinion about
        where it is. Both legs carry the identical window -- that is what makes them legs.
        """
        return self.after.start

    @property
    def duration(self) -> float:
        return self.after.duration

    @property
    def end(self) -> float:
        return self.after.end

    @property
    def label(self) -> str:
        """Both shots, in the order they play, for a sentence that names the boundary."""
        return f"{self.before.label} into {self.after.label}"


def take_cut_refusal(
    *, label: str, offset: float, duration: float, take_seconds: float | None
) -> str | None:
    """The cut, judged against the take it is a cut of. One sentence, or `None`.

    Two ways a cut can fail to land inside its take and they are mutually exclusive, which is
    why they are one function rather than two: it begins before the take does — a nudge pulled
    further back than the recorded lead — or it runs off the end — a nudge pushed past the
    tail, or the lead of a last shot that has already spent the whole over-render margin. A cut
    cannot be both, so at most one sentence is ever true and the caller appends or raises it.

    Extracted from `assembly_refusals` so that the export and the **preview** cannot answer
    this differently. The export collects the sentence into its comprehensive report; the
    preview raises it alone, because a preview is one Shot and there is no second fault to
    collect. Two callers, one rule, one wording — a preview that rendered a cut the export
    refuses would be a picture of a video that will never exist, and a preview that refused one
    the export accepts would ration a Director over nothing.

    `None` is the green light. It is also the overrun answer for a take whose length could not
    be measured: an undecidable overflow must not fabricate a refusal, which is the rule
    `assembly_refusals` has always applied to a `take_seconds` of `None`. The negative test
    needs no measurement at all — a cut before a take's first frame is impossible whatever the
    take turns out to hold — so it is decided even for a file nothing could probe.

    The overrun's slack is `BOUNDARY_TOLERANCE_SECONDS` — half a frame — because a take that
    supplies *exactly* its window supplies it. That equality is not a corner case:
    `over_render_lead`'s overflow branch grows the lead until the take's tail lands on the
    song's last second, so the last shot of every song is cut to the take's final frame, and
    refusing at equality would refuse the ordinary case. The negative side gets no slack, and
    that asymmetry is deliberate: zero is a real offset that every shot at 0.0 s carries, and
    anything below it is a number the Director's own client clamps away.
    """
    if offset < 0:
        return ASSEMBLY_OFFSET_NEGATIVE_REFUSAL.format(shot=label, behind=-offset)
    if take_seconds is None:
        return None
    needed = offset + duration
    if needed <= take_seconds + BOUNDARY_TOLERANCE_SECONDS:
        return None
    return ASSEMBLY_OFFSET_OVERRUN_REFUSAL.format(
        shot=label,
        take=take_seconds,
        offset=offset,
        duration=duration,
        needed=needed,
    )


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
        # needs, both are decidable only here — the client clamps, but the manifest is
        # writable by clients that do not — and both are `take_cut_refusal`'s, so the
        # preview route refuses the same two cuts in the same two sentences.
        cut = take_cut_refusal(
            label=clip.label,
            offset=clip.offset,
            duration=clip.duration,
            take_seconds=clip.take_seconds,
        )
        if cut is not None:
            refusals.append(cut)
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

    #: **One ordered list, holding either kind of entry** (R-39, AD-18). A `TransitionClip` is a
    #: union member here rather than a sibling list, because `frames` is zipped 1:1 with this and
    #: the grid's guarantee telescopes over one sequence of boundaries. Every consumer decides
    #: what a transition entry means to it, and the type is what makes that decision compulsory.
    clips: list[ClipWindow | TransitionClip]
    #: **Every count is positive**, and `assembly_plan` is where that is made true rather than
    #: hoped for (AD-18's 2026-08-29 amendment, whose "asserted at the split now" was false until
    #: 2026-08-30 -- the assertion existed in one test, on one fixture). A negative count is a
    #: window that runs backwards, which keeps `sum(frames)` correct by cancelling against itself
    #: and is the one way the frame rule can hold while the export ships the wrong Shot; a zero
    #: count is an entry that lays nothing and is dropped. See `assembly_plan`.
    frames: list[int]
    width: int
    height: int
    song_seconds: float
    #: Every Transition the manifest asked for that this plan did **not** compose, one sentence
    #: each (R-37). Empty is the ordinary answer. The plan is still complete and still assembles;
    #: these are recorded on `ExportLook.transitions` so a boundary that quietly stayed a hard cut
    #: is not a silence. See `TRANSITION_CROWDED_REFUSAL`.
    transition_refusals: list[str] = dataclass_field(default_factory=list)

    @property
    def total_frames(self) -> int:
        return sum(self.frames)


def _split_frames(
    before: ClipWindow,
    entries: Sequence[ClipWindow | TransitionClip],
    position: int | None,
    overlap_start: float,
    overlap_end: float,
) -> tuple[int, int, int]:
    """The three stretches an Overlap's split would lay, in the order they play, as frames.

    **This is the whole of the decision `_paired_transitions` makes**, and it is a measurement
    rather than a description. A boundary that blends becomes exactly three consecutive stretches
    of the plan -- the outgoing Shot's own frames up to the Overlap, the blend, the incoming
    Shot's own frames after it -- and each is read off `clip_frames_on_grid` at the very
    boundaries the split writes, from the entries the resolution loop actually produced. Nothing
    here consults a window; the windows are what lied.

    **A stretch that is not in the plan at all is 0**, because that is how many frames it
    contributes. `position` is `None` when the incoming Shot has no entry beginning at the
    Overlap; `entries[position - 1]` is not the outgoing Shot when the outgoing Shot has no
    surviving frames before the blend -- which is the mirror nesting the old one-directional check
    could not see, and which reached the split with the Overlap being the *whole* of the outgoing
    Shot. `A[0,4]` under `B[0,10]` with a dissolve on A opened the video with four seconds of a
    Shot that renders nothing without the transition, recorded as an ordinary `shot_a=dissolve`.

    The entry before the incoming Shot's head is the outgoing Shot's tail whenever that tail
    exists: everything starting before the Overlap is at or before `before` in song order, and
    `before` covers the instant, so later-on-top makes `before` the visible picture there.
    """
    blend = clip_frames_on_grid(overlap_start, overlap_end)
    if position is None:
        return 0, blend, 0
    head = entries[position]
    assert isinstance(head, ClipWindow)
    lead = entries[position - 1] if position else None
    outgoing = (
        clip_frames_on_grid(lead.start, overlap_start)
        if isinstance(lead, ClipWindow) and lead.shot_id == before.shot_id
        else 0
    )
    return outgoing, blend, clip_frames_on_grid(overlap_end, head.end)


def _degenerate_refusal(
    *,
    before: ClipWindow,
    after: ClipWindow,
    ordered: Sequence[ClipWindow],
    overlap_start: float,
    overlap_end: float,
    outgoing: int,
    blend: int,
    incoming: int,
) -> str:
    """What to call the emptiness `_split_frames` found. **It decides nothing** (2026-08-30).

    Every sentence here is true of a boundary the rule has already refused, and the rule refused
    it for the same reason whichever sentence comes back. That separation is the correction: both
    of the older conditions used to *be* the decision, and both were wrong about it -- the nested
    one in the direction it did not cover, the crowding count in the half-frame band it excludes.
    The count is still excluded from the sentence's arithmetic, because a boundary written twice
    within half a frame is one boundary and always was; what has changed is that it no longer
    lets a plan through.

    Order is most specific first. A nested window is named as nested **in whichever direction it
    is nested**, since `TRANSITION_NESTED_REFUSAL`'s `{before}` is the outer Shot and the outer
    Shot is not always the earlier one. Then the crowding count, where there really are more than
    two pictures over the stretch. Then the numbers, for the two shapes that are neither.
    """
    if incoming <= 0 and after.end - overlap_end <= BOUNDARY_TOLERANCE_SECONDS:
        outer, inner = before, after
    elif outgoing <= 0 and overlap_start - before.start <= BOUNDARY_TOLERANCE_SECONDS:
        outer, inner = after, before
    else:
        outer = inner = None
    if outer is not None and inner is not None:
        return TRANSITION_NESTED_REFUSAL.format(
            before=outer.label,
            after=inner.label,
            start=outer.start,
            end=outer.end,
            inner_start=inner.start,
            inner_end=inner.end,
        )
    covering = [
        clip
        for clip in ordered
        if clip.start < overlap_end - BOUNDARY_TOLERANCE_SECONDS
        and clip.end > overlap_start + BOUNDARY_TOLERANCE_SECONDS
    ]
    if len(covering) != 2:
        return TRANSITION_CROWDED_REFUSAL.format(
            before=before.label,
            after=after.label,
            start=overlap_start,
            end=overlap_end,
            count=len(covering),
        )
    return TRANSITION_EMPTY_SPLIT_REFUSAL.format(
        before=before.label,
        after=after.label,
        start=overlap_start,
        end=overlap_end,
        outgoing=outgoing,
        blend=blend,
        incoming=incoming,
    )


def _paired_transitions(
    ordered: list[ClipWindow],
    resolved: list[ClipWindow],
    transitions: Mapping[str, TransitionChoice],
) -> tuple[list[ClipWindow | TransitionClip], list[str]]:
    """The resolved clips with each authored Overlap turned into a blend, and what was refused.

    **This runs after the resolution loop, not instead of it**, and that is the whole reason the
    grid is untouched. The loop above has already cut the earlier clip at the later one's start
    (the Director's layers ruling, 2026-08-20), so the Overlap's seconds live entirely inside the
    *later* clip's resolved range. Making a transition is therefore one split of one entry --
    `[b_start, a_end]` comes out as a `TransitionClip` and `[a_end, b_end]` stays as the later
    clip -- plus one `replace` producing the earlier Shot's tail as the first leg. Every boundary
    is still some clip's own start or its own end (`a_end` is the earlier Shot's end, `b_start`
    the later Shot's start), so `clip_frames_on_grid` telescopes exactly as it did and
    `sum(frames)` is the number it was.

    That is what "no new geometry" means, and what it does not: **`assembly_plan` emits two
    entries at an Overlap today and three with a transition** (R-39's correction to AD-18's
    reading). The third entry is a change to what this function emits; the grid is what stays.

    `transitions` is keyed by the **outgoing** Shot's id, which is AD-30: `transition_out` on the
    earlier Shot is authoritative and the later Shot's `transition_in` is a mirror. Only the
    outgoing field is read here, so a manifest whose pair disagrees has a decidable export.

    **One rule decides whether a boundary blends, and it is a measurement of the split rather
    than a description of a geometry** (2026-08-30). `_split_frames` lays the three stretches the
    split would produce -- the outgoing Shot's own frames, the blend, the incoming Shot's own
    frames -- reads each off `clip_frames_on_grid`, and the transition is composed only when all
    three are positive. Otherwise the boundary is returned to the hard cut it already is and one
    sentence is recorded.

    **Why one rule and not three conditions.** Until 2026-08-30 there were two conditions, each
    written for a geometry somebody had enumerated, and both asked their question of a *different*
    object than the split used: the nested branch tested `after.end`, the incoming Shot's full
    window, while the third entry's length came from `head.end`, its end *after* the resolution
    loop truncated it; and the crowding count applied `BOUNDARY_TOLERANCE_SECONDS` inwards, so a
    third clip in the half-frame band below the Overlap's end was not counted and still truncated
    the head. Three degenerate shapes got through -- a nested **outgoing** Shot, which the nested
    branch was one-directional about, and both bands above -- and every one of them satisfied
    `sum(plan.frames) == round(song * 24)`, because a window that runs backwards cancels against
    itself. **That is the one way this project's oldest invariant can hold while the export ships
    something a Director never authored**, and no fourth condition would have been the last one.
    Asking the split what it produced is answerable once and covers the shapes nobody enumerated,
    including two this function refuses today that no `covering` count can see.

    The two older sentences are kept, and they are now **names for what the measurement found**
    rather than tests of their own (see `_degenerate_refusal`). R-37 asks for both, a nested
    boundary really is nested and a crowded one really is crowded, and a Director reading
    `TRANSITION_NESTED_REFUSAL` gets the remedy `timeline.SNAP_NESTED` gives for the same picture.
    Where neither describes the geometry, `TRANSITION_EMPTY_SPLIT_REFUSAL` states the three
    numbers the rule measured.
    """
    if not transitions:
        return list(resolved), []
    entries: list[ClipWindow | TransitionClip] = list(resolved)
    refusals: list[str] = []
    for index, before in enumerate(ordered[:-1]):
        choice = transitions.get(before.shot_id)
        if choice is None:
            continue
        after = ordered[index + 1]
        overlap_start, overlap_end = after.start, before.end
        if overlap_end - overlap_start <= BOUNDARY_TOLERANCE_SECONDS:
            # No Overlap, so no paired transition. A stored type on a boundary with no Overlap is
            # a **one-sided** treatment of this clip's own frames (AD-19, FX-16) -- story 11.4,
            # shipped 2026-08-29 -- and it is composed in `app._compose_one_sided_transitions`,
            # onto the clip's own chain, with no entry here and nothing refused. Nothing about
            # this module changes when a boundary is one-sided, which is the property that let
            # story 11.4 be built without going near the frame grid: it is a filter spliced into
            # an argv this function's output was already going to produce.
            #
            # **This stays a question about seconds and not about frames**, deliberately, even
            # though the rule below is about frames. `app._boundary_is_overlapped` and
            # `routes/shots.replace_shot_transitions` ask it in seconds too, and a boundary the
            # three of them disagreed about would be composed as a blend here *and* as a one-sided
            # treatment there -- one boundary treated twice. An Overlap longer than half a frame
            # that still lays no frames on the grid is therefore not sent down this branch; it is
            # measured by the rule below and refused with the number in the sentence.
            continue
        # The later Shot's resolved range that begins at the Overlap: the only entry the split
        # touches. `None` is not a fault to guess about -- it is nothing of the incoming Shot at
        # this boundary, which the rule below counts as the zero frames it is.
        position = next(
            (
                spot
                for spot, entry in enumerate(entries)
                if isinstance(entry, ClipWindow)
                and entry.shot_id == after.shot_id
                and abs(entry.start - overlap_start) <= BOUNDARY_TOLERANCE_SECONDS
            ),
            None,
        )
        # **The one rule.** Three stretches, measured on the grid the export writes, off the
        # entries the resolution loop actually produced. All three positive or no blend.
        outgoing, blend, incoming = _split_frames(
            before, entries, position, overlap_start, overlap_end
        )
        if min(outgoing, blend, incoming) <= 0:
            refusals.append(
                _degenerate_refusal(
                    before=before,
                    after=after,
                    ordered=ordered,
                    overlap_start=overlap_start,
                    overlap_end=overlap_end,
                    outgoing=outgoing,
                    blend=blend,
                    incoming=incoming,
                )
            )
            continue
        assert position is not None
        head = entries[position]
        assert isinstance(head, ClipWindow)
        entries[position : position + 1] = [
            TransitionClip(
                # The earlier Shot's tail, from its **own** window rather than from its truncated
                # segment: the segment stops at `overlap_start`, and these are the frames beyond
                # it. `offset` advances by exactly the seconds skipped, which is the rule the
                # resolution loop applies to every split it makes.
                before=replace(
                    before,
                    start=overlap_start,
                    duration=overlap_end - overlap_start,
                    offset=before.offset + (overlap_start - before.start),
                ),
                after=replace(
                    head, start=overlap_start, duration=overlap_end - overlap_start
                ),
                choice=choice,
            ),
            replace(
                head,
                start=overlap_end,
                duration=head.end - overlap_end,
                offset=head.offset + (overlap_end - overlap_start),
            ),
        ]
    return entries, refusals


def assembly_plan(
    clips: list[ClipWindow],
    song_seconds: float,
    dimensions: dict[str, tuple[int, int]],
    transitions: Mapping[str, TransitionChoice] | None = None,
) -> AssemblyPlan:
    """The plan for a set of clips that passed `assembly_refusals`.

    `dimensions` maps shot id → the take's probed (width, height). The normalization
    target is the largest-area take present: mid-iteration plans legitimately mix draft
    and master takes, and scaling everything *up* to the best take present degrades no
    clip that is already there. Aspect is preserved and padded, never stretched — the
    house resolutions differ in aspect (640×384 is 5:3, 1056×608 is ~1.74:1), and a 5 %
    silent stretch is a defect with a face in it.

    **Nothing leaves here with a non-positive frame count** (AD-18, made true 2026-08-30). The
    guarantee is on this function's *output* rather than on any enumeration of geometries, because
    every enumeration so far has been short by one: a negative count is raised, a zero count is
    dropped, and the transition split refuses any boundary that would produce either. See the
    comment on the loop below for why those are two answers rather than one, and
    `_paired_transitions` for the rule that keeps the negative branch unreachable.
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
    # The Overlaps the Director authored a blend for, turned into entries. `entries` is the same
    # boundaries as `resolved` in the same order -- one of them split in two -- so the frame sum
    # below is computed from the same telescoping sequence either way. See `_paired_transitions`.
    entries, transition_refusals = _paired_transitions(
        ordered, resolved, transitions or {}
    )
    frames = [clip_frames_on_grid(clip.start, clip.end) for clip in entries]
    # **The invariant, on this function's own output** (AD-18's 2026-08-29 amendment, made true
    # 2026-08-30). `all(count > 0 for count in plan.frames)` holds on every plan this function
    # returns, and it holds in two halves because the two failures are not the same failure.
    #
    # A **negative** count cannot happen and is a defect if it does: it means an entry runs
    # backwards, which is the one shape that keeps `sum(frames)` correct while the plan is wrong,
    # because the window cancels against itself. Downstream it is `-frames:v -1`, which ffmpeg
    # **ignores at rc 0 with no warning** and answers by encoding the entire rest of the take
    # (measured 2026-08-30: asked -1, wrote 142 frames), which `concat` then joins and `-shortest`
    # trims back to the song, so `verification_problems` finds nothing and the export ships half
    # its running time as the wrong Shot. There is nothing to salvage and nothing to report to a
    # Director, because no Director authored it: it is raised.
    #
    # A **zero** count can happen without any transition at all, and is not a defect: a Shot
    # shorter than a frame nested inside another resolves to a segment longer than
    # `BOUNDARY_TOLERANCE_SECONDS` whose two ends round to the same grid frame -- `A[0,10]` with
    # `B[0.483333, 0.516667]` gives `[12, 0, 228]`. It lays no frames, so it is dropped, and the
    # drop is **provably sum-neutral**: the entry contributed 0, and its neighbours telescope
    # across it because `round(start * 24) == round(end * 24)` is exactly what made it 0. What it
    # costs today is an intermediate with no video stream in the concat list (`-frames:v 0` writes
    # a 261-byte file that `ffprobe` reports no streams for, at rc 0), which is one more thing
    # answering rc 0 while meaning nothing.
    for entry, count in zip(entries, frames, strict=True):
        if count < 0:
            raise ValueError(
                ASSEMBLY_NEGATIVE_FRAMES_ERROR.format(
                    frames=count, label=entry.label, start=entry.start, end=entry.end
                )
            )
    if not all(frames):
        laid = [pair for pair in zip(entries, frames, strict=True) if pair[1]]
        entries = [entry for entry, _ in laid]
        frames = [count for _, count in laid]
    # A transition contributes both legs' Shots, and the normalization target is still the
    # largest-area take present: the segment is rendered at the export's grid like every other
    # intermediate, and a leg is one of the takes this already considered.
    width, height = max(
        (dimensions[clip.shot_id] for clip in resolved),
        key=lambda size: size[0] * size[1],
    )
    return AssemblyPlan(
        clips=entries,
        frames=frames,
        width=width,
        height=height,
        song_seconds=song_seconds,
        transition_refusals=transition_refusals,
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

#: The Monitor's Preview Clip (AD-23), and **deliberately not a member of `EXPORT_PRESETS`
#: below**: it is not a delivery build and must never be selectable at the assemble route. It
#: lives here rather than in `app.py` because everything else that decides an encode lives here,
#: and a second place that names an x264 preset is how `draft` and `master` would drift apart.
#:
#: `ultrafast` / CRF 28, and not a hardware encoder. Measured 2026-08-21: half-dimension preview
#: clips cost 270 ms through libx264 and 403–527 ms through NVENC, because encoder initialisation
#: dominates a sub-second job. The picture quality this trades away is the point — a preview is
#: judged for its *grade*, and it is re-rendered on every change, so it buys the budget FX-NFR-6
#: sets rather than spending it.
#:
#: Nothing else about the preview's argv differs from the export's: `trim_args` builds both, from
#: the same stages `effects.build_effect_stages` composes. Reduced in geometry and in these two
#: literals, identical in every other respect — which is the only reason a preview predicts
#: anything about the file the export will write.
PREVIEW_PRESET = ExportPreset(name="preview", x264_preset="ultrafast", crf="28")

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


def normalized_stages(
    *,
    head: Sequence[str],
    width: int,
    height: int,
    geometry_stages: Sequence[str] = (),
    treatment_stages: Sequence[str] = (),
) -> list[str]:
    """One clip's whole filter chain: whatever cuts it, its look, and the normalization.

    **Extracted so `trim_args` and `transition_segment_args` cannot drift**, which is not a tidying
    argument. R-38 requires the transition segment be concat-identical to every other intermediate
    -- same geometry, same rate, same SAR, same pixel format -- because the join is `-c:v copy`
    (FX-NFR-2) and a segment that differed in any of the four would either fail the copy or ride
    into the stream as a second encoding of the same timeline. Two functions writing that tail
    separately is exactly how one of them would later gain a stage the other did not.

    `head` is the only thing that differs between the two callers, and the difference is real:
    `trim_args` opens the cut with `trim=start_frame=` and closes it with `-frames:v`, which caps
    the process's single output. A segment has **two** legs inside one `-filter_complex` and
    `-frames:v` can only cap what comes out of the graph, so each leg has to close itself with an
    `end_frame`. See `transition_segment_args` for the measurement that makes that mandatory
    rather than careful.

    The two insertion points are AD-17's and are unchanged: `geometry_stages` before `scale` so a
    punch-in samples the take's own pixels, `treatment_stages` after `scale` and before `pad` so
    grain and a vignette leave the letterbox bars at pure black.
    """
    return [
        *head,
        *geometry_stages,
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        *treatment_stages,
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
        f"fps={ASSEMBLY_FPS}",
        "setsar=1",
        "format=yuv420p",
    ]


def trim_args(
    source: Path,
    dest: Path,
    frames: int,
    width: int,
    height: int,
    offset: float = 0.0,
    preset: ExportPreset = DRAFT_PRESET,
    geometry_stages: Sequence[str] = (),
    treatment_stages: Sequence[str] = (),
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

    **The two effect insertion points, and there are only two** (AD-17). `geometry_stages`
    goes after the trim pair and *before* `scale`, so a punch-in samples the take's own
    pixels rather than resampling an already-scaled frame. `treatment_stages` — texture,
    grade and stylize, already in their fixed order — goes after `scale` and *before* `pad`,
    so grain and a vignette treat the picture and leave the letterbox bars at pure black
    (measured 2026-08-21: after `pad` the bar samples RGB `(1,1,5)`, before it `(0,0,0)`).
    Everything from `pad` onward is untouched, and both default to empty, so a project with
    no effects builds the byte-identical argv this function has always built.

    Neither group is composed here. `effects.py` owns the catalogue, the validation and the
    family ordering, and hands this function two lists of finished strings — which is why
    this module imports nothing from it and the grid arithmetic below cannot be reached by
    an effect at all.
    """
    skip = round(offset * ASSEMBLY_FPS)
    filters = ",".join(
        normalized_stages(
            head=[f"trim=start_frame={skip}", "setpts=PTS-STARTPTS"]
            if skip > 0
            else [],
            width=width,
            height=height,
            geometry_stages=geometry_stages,
            treatment_stages=treatment_stages,
        )
    )
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


#: The two link labels a transition segment's legs come out on. Deliberately not `fx`-prefixed:
#: `effects._branch_stage` owns every label beginning `fx`, and a segment carries two of its
#: chains at once (R-41).
TRANSITION_LEG_LABELS = ("xfa", "xfb")

#: What closes a transition segment, after the `xfade`, and it is **not** optional.
#:
#: Measured 2026-08-28 on this machine's ffmpeg 7.0: two legs each ending `format=yuv420p`, joined
#: by `xfade` with nothing after it, encode as **`yuv444p`, profile High 4:4:4 Predictive** -- rc
#: 0, nothing at `-v warning`, correct frame count, correct size, correct rate. Every other
#: intermediate is `yuv420p` / High, and the join is `-c:v copy`, so that segment goes into the
#: export as a mid-stream chroma-format switch inside one copied track. `ffmpeg -f concat` accepts
#: it, reports rc 0, and writes a file whose container declares the *first* stream's `yuv420p`.
#: The `xfade`'s own output format has to be pinned back, and this is the pin.
#:
#: **`fps` is deliberately absent from it.** Both legs already close on `fps={ASSEMBLY_FPS}` and
#: the measured output rate is 24/1; a rate filter placed *downstream of a framesync filter* is
#: the exact shape `effects.BRANCH_FRAME_GUARD` exists to compensate for, and there is no reason
#: to introduce one where nothing needs it.
TRANSITION_SEGMENT_TAIL = ("setsar=1", "format=yuv420p")

#: How many frames of each Shot a **boundary preview** shows on either side of the blend, so the
#: outgoing Shot, the transition and the incoming Shot play as one continuous piece (FX-21, story
#: 11.5). Half a second at 24 fps, and it is a ceiling: the caller clamps it to the frames each
#: neighbour actually has on the grid, because a leg cannot be read from frames its Shot does not
#: cover.
#:
#: **Frames rather than seconds**, for `effects.ONE_SIDED_TRANSITION_FRAMES`' reason: the number
#: becomes a `trim=start_frame=`/`end_frame=` pair and an `xfade` offset, and a seconds figure
#: would have to be rounded back onto this grid by whoever read it.
#:
#: **Twelve is chosen against the measured budget rather than by taste.** FX-NFR-6 allows one
#: second; a 2 s window around an `xfade` costs 143-187 ms (`docs/BUILD-HANDOFF.md`). A 12-frame
#: margin either side of a half-second blend is a 1.5 s window, which is inside that measurement
#: and leaves the Director enough of each Shot to see what the blend is between.
TRANSITION_PREVIEW_MARGIN_FRAMES = 12


def xfade_stage(xfade: str, frames: int, *, offset_frames: int = 0) -> str:
    """The `xfade` filter, written once, for the export's segment and for its preview.

    **This is what "the transition previewed is the export's, by name and by duration" is made
    of** (FX-NFR-3, story 11.5). Both callers reach the same two numbers through this function, so
    the claim is checkable by string comparison on the composed graphs rather than by reading two
    argv builders and believing they agree -- which is the shape every other generated render
    input in this project is already pinned by.

    `offset_frames` is where the blend starts inside the *first* leg, and it is the one thing the
    two callers differ on. An export's segment **is** the Overlap, so the blend starts at its first
    frame and the offset is zero. A preview spans the boundary -- some of the outgoing Shot, the
    blend, some of the incoming Shot, one continuous piece -- so the blend starts `offset_frames`
    in. Neither the name nor the duration moves.

    **Zero is spelled `0` and not `0.000000`, deliberately.** That is the text every export argv
    this application has ever written carries, and a preview is not entitled to move the export's
    own bytes to make its own arithmetic tidier. The pinned-argv tests in `tests/test_assembly.py`
    are the record of that text.
    """
    offset = f"{offset_frames / ASSEMBLY_FPS:.6f}" if offset_frames else "0"
    return (
        f"xfade=transition={xfade}:duration={frames / ASSEMBLY_FPS:.6f}:offset={offset}"
    )


def transition_segment_args(
    before: Path,
    after: Path,
    dest: Path,
    frames: int,
    width: int,
    height: int,
    xfade: str,
    *,
    before_offset: float = 0.0,
    after_offset: float = 0.0,
    preset: ExportPreset = DRAFT_PRESET,
    before_geometry: Sequence[str] = (),
    before_treatment: Sequence[str] = (),
    after_geometry: Sequence[str] = (),
    after_treatment: Sequence[str] = (),
    lead_frames: int = 0,
    tail_frames: int = 0,
) -> list[str]:
    """Two takes -> one blended intermediate: the Overlap's frames, from both Shots, in one run.

    AD-18's segment and R-38's shape. Each leg is read from its **own** take, put through its
    **own** full effect chain (R-41), normalized by the same `normalized_stages` every other
    intermediate is built from, and the two are joined by one `xfade`. The result is
    concat-identical to a `trim_args` output, so the join keeps `-c:v copy` and the export gains
    no second generation of loss (FX-NFR-2).

    **Both legs close themselves with `end_frame`, and that is the whole safety of this function.**

    Measured 2026-08-28 on ffmpeg 7.0, and reproduced independently before the slice was written:
    `xfade` with legs of **unequal length silently truncates to the shorter one**. Thirteen-frame
    and twelve-frame legs give **twelve frames out, rc 0, nothing at `-v warning`** -- and
    `-frames:v 13` does **not** catch it, because a frame cap caps from above only. There is no
    `T` flag on any `xfade` option, so R-29's crash class does not reach here; the silent-short
    class does, and it lands on the one rule this project may never break.

    Three things follow, and all three are in the argv below rather than in a comment:

    * each leg carries `trim=start_frame=S:end_frame=S+frames`, so the two legs are the same
      length by construction rather than by the sources happening to agree;
    * `-frames:v frames` stays, as the cap from above it has always been;
    * and **the caller asserts the rendered frame count**, because neither of the two above can
      see a leg that came up short. `tests/test_assembly_route.py` renders a segment and counts
      its frames against `clip_frames_on_grid`; a pinned argv is necessary here and is not
      sufficient.

    A leg whose effect chain branches loses a frame at its own `fps` stage and gets
    `effects.BRANCH_FRAME_GUARD` from `build_effect_stages` exactly as an ordinary clip does --
    **and an `xfade` graph is two branched legs.** Measured the same day with the guard suppressed
    on both legs: thirteen frames asked for, **twelve** written, rc 0.

    `duration` is the segment's own length on the assembly grid -- `frames / ASSEMBLY_FPS`, six
    decimals, the formatter every generated render input in this project already uses -- and
    `offset=0`, because the blend *is* the whole segment (AD-19: a paired transition's duration is
    the Overlap's duration, and there is no second source for that number).

    **`lead_frames` and `tail_frames` are the boundary preview's, and they default to nothing so
    the export's argv is the argv this function has always written** (story 11.5). A preview of a
    transition has to span the boundary -- the outgoing Shot, the blend, the incoming Shot, as one
    continuous piece (FX-21) -- and that is the same graph with each leg extended on its own side
    and the `xfade` moved off frame zero. The blend itself does not move: `xfade_stage` writes the
    name and the duration for both callers, which is what makes *"the transition previewed is the
    export's"* a string comparison rather than a reading of two builders.

    Three things follow, and the third is why they are frames:

    * the first leg gains `lead_frames` **before** the blend and still ends where the blend ends,
      so `end_frame` is untouched and the leg is `lead_frames + frames` long;
    * the second leg gains `tail_frames` **after** it and still starts where the blend starts;
    * `-frames:v` becomes the whole window, so the cap goes on covering the output rather than one
      third of it. `xfade`'s silent truncation to the shorter leg is measured above and it does not
      go away because the legs are now deliberately unequal -- `offset + duration` is exactly the
      first leg's length and `duration` exactly the second's head, so the graph is still fully
      determined by construction, and `tests/test_shot_preview.py` counts the decoded frames.

    **The caller clamps, and it clamps against the plan.** `before_offset` minus `lead_frames`
    reaching behind the take's first frame would be the negative-trim failure `take_cut_refusal`
    exists for -- silent, at rc 0, and a picture of the wrong seconds. The frames each neighbour
    really has are `plan.frames` either side of the `TransitionClip`, which is the only source that
    has already survived `assembly_refusals`.
    """
    lead, follow = TRANSITION_LEG_LABELS
    skips = (round(before_offset * ASSEMBLY_FPS), round(after_offset * ASSEMBLY_FPS))
    legs = [
        ",".join(
            normalized_stages(
                head=[
                    f"trim=start_frame={start}:end_frame={stop}",
                    "setpts=PTS-STARTPTS",
                ],
                width=width,
                height=height,
                geometry_stages=geometry,
                treatment_stages=treatment,
            )
        )
        for start, stop, geometry, treatment in (
            (skips[0] - lead_frames, skips[0] + frames, before_geometry, before_treatment),
            (skips[1], skips[1] + frames + tail_frames, after_geometry, after_treatment),
        )
    ]
    graph = (
        f"[0:v]{legs[0]}[{lead}];"
        f"[1:v]{legs[1]}[{follow}];"
        f"[{lead}][{follow}]"
        + xfade_stage(xfade, frames, offset_frames=lead_frames)
        + ","
        + ",".join(TRANSITION_SEGMENT_TAIL)
    )
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        before.as_posix(),
        "-i",
        after.as_posix(),
        "-filter_complex",
        graph,
        "-frames:v",
        str(lead_frames + frames + tail_frames),
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

    *Within* one frame, and the `>` that says so is deliberate — see
    `COVERAGE_TOLERANCE_SECONDS`, which carries the 2026-08-26 re-examination and the
    measurement showing that `>=` would change no outcome at all.
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
