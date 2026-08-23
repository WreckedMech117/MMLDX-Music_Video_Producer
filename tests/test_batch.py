"""The readiness gate's own truth table, over the pure report and nothing else.

`tests/test_timeline.py` is the precedent: a pure module is tested directly, with an in-memory
`Project` and no client, so the rules are asserted rather than inferred from a route's status code.
"""

from datetime import timedelta
from pathlib import Path
from typing import get_args

import pytest

from music_video_producer.batch import (
    JOB_LOST_WITH_QUEUE,
    JOB_NEVER_SUBMITTED,
    JOB_SUPERSEDED,
    JOB_TIMING_FROM_COMFY,
    JOB_TIMING_FROM_RECORD,
    JOB_TIMING_UNMEASURED,
    MISSING_TICKS_LIMIT,
    NEAR_DUPLICATE_OVERLAP,
    NOTE_KIND_PROMPT,
    NOTE_KIND_SAMENESS,
    NOTE_KIND_STALE_MAP,
    NOTE_KIND_TAKE_UNCOVERED,
    NOTE_KIND_WINDOW_LONG,
    NOTE_KIND_WINDOW_SHORT,
    PENDING_SUBMISSION_PROMPT_ID,
    PLACEHOLDER_PROMPT,
    PLAN_WITHOUT_SHOTS,
    READINESS_REFUSAL,
    REFUSAL_NAME_LIMIT,
    SHOT_WINDOW_ABOVE_BAND,
    SHOT_WINDOW_BEFORE_TAKE,
    SHOT_WINDOW_BELOW_BAND,
    SHOT_WINDOW_PAST_TAKE,
    SHOT_WITH_PLACEHOLDER_PROMPT,
    SHOT_WITH_STALE_REFERENCE_MAP,
    SHOT_WITHOUT_PROMPT,
    SHOTS_LACK_VARIANCE,
    SHOTS_SHARE_ONE_PROMPT,
    TAKE_COVERAGE_TOLERANCE_SECONDS,
    TERMINAL_JOB_STATUSES,
    VARIANCE_WARNING_LIMIT,
    AssetRenderState,
    ShotRenderState,
    SongRenderState,
    _overlap,
    _words,
    accept_submission,
    apply_job_history,
    format_duration,
    prompt_is_missing,
    prompt_rejection,
    queue_locations,
    readiness_refusal,
    readiness_report,
    reconcilable_jobs,
    reconcile_render_jobs,
    render_status_report,
    render_timing_summary,
    shot_label,
    stamp_job_settled,
    supersede_target_jobs,
    take_coverage_note,
    window_band_note,
)
from music_video_producer.comfy import ComfyError, HistoryResult
from music_video_producer.models import (
    SHOT_MODE_SPECS,
    Asset,
    AssetCitation,
    JobStatus,
    Project,
    RenderJob,
    Shot,
    Song,
    VisionInspectionRecord,
)
from music_video_producer.reference_map import (
    STALE_REFERENCE_MAP_CAUSE,
    STALE_REFERENCE_MAP_CONSEQUENCE,
    STALE_REFERENCE_MAP_REMEDY,
    reference_map_sentence,
    stale_reference_map,
)


def plan(*prompts: str) -> Project:
    """A project of consecutive, non-overlapping Shots with stable ids, one per prompt."""
    project = Project(name="Readiness")
    project.shots = [
        Shot(id=f"shot_{index}", start=index * 10, duration=5, prompt=prompt)
        for index, prompt in enumerate(prompts)
    ]
    return project


# Three prompts of eight, nine and ten *distinct* words -- no word repeats in any of them, so
# the arithmetic below is the arithmetic of the sets. Each threshold case adds one unique word
# to one of them, which is what makes these assertions about the ratio rather than about "these
# two look alike": 8 -> 0.8 (under), 9 -> exactly 0.9 (the `>` / `>=` boundary), 10 -> 0.909
# (over).
EIGHT_WORDS = "a singer walks the corridor while sodium light"
NINE_WORDS = f"{EIGHT_WORDS} rakes"
TEN_WORDS = f"{NINE_WORDS} shoulders"


def test_an_empty_prompt_blocks_and_the_report_names_the_shot():
    report = readiness_report(plan("A singer turns toward camera", ""))

    assert report.ready is False
    assert report.blocked_ids() == ["shot_1"]
    assert report.blocking[0].reason == SHOT_WITHOUT_PROMPT
    # The prompted Shot is still counted: the report says what is fit, not only what is not.
    assert report.ready_count == 1
    assert report.shot_count == 2


def test_a_whitespace_only_prompt_is_treated_exactly_as_an_empty_one():
    """Matching `expansion_rejection`'s `if not prompt.strip()`.

    Three spaces and a newline is not a prompt, and ComfyUI cannot tell it from `""` -- both
    spend a full GPU pass and return noise. Asserted against the empty-prompt report rather than
    on its own, so a guard that special-cased `""` alone cannot pass this.
    """
    blank = readiness_report(plan("A singer turns toward camera", ""))
    whitespace = readiness_report(plan("A singer turns toward camera", "   \n\t"))

    assert whitespace == blank
    assert prompt_is_missing(Shot(start=0, duration=5, prompt="   \n\t")) is True
    assert prompt_is_missing(Shot(start=0, duration=5, prompt=" x ")) is False


def test_the_new_shot_placeholder_blocks_and_says_which_kind_of_blank_it_is():
    """The application's own placeholder is the most common unrenderable state there is.

    `app.js` stamps `"New shot"` onto every Shot it creates and duplicating a Shot copies it, so
    a plan reaches submission carrying it by *default* -- while reaching `""` takes a deliberate
    deletion. A gate that blocks `""` and waves the placeholder through blocks the rarer half of
    the problem and reports the plan ready for the commoner one.

    The reason is its own sentence rather than "this shot has no prompt", because the two are
    different situations: one shot was never written, the other had its text cleared.
    """
    report = readiness_report(plan("A singer turns toward camera", PLACEHOLDER_PROMPT))

    assert report.ready is False
    assert report.blocked_ids() == ["shot_1"]
    assert report.blocking[0].reason == SHOT_WITH_PLACEHOLDER_PROMPT
    assert SHOT_WITH_PLACEHOLDER_PROMPT != SHOT_WITHOUT_PROMPT
    assert report.ready_count == 1

    # Case and spacing are collapsed first, so a copy that picked up stray spacing is caught too.
    assert prompt_rejection("  new    SHOT ") == SHOT_WITH_PLACEHOLDER_PROMPT
    # And a real prompt that merely starts with those words is not the placeholder.
    assert prompt_rejection("New shot on the corridor, sodium backlight") == ""


def test_status_is_not_consulted_anywhere_in_the_gate():
    """Nothing in the shipped UI ever writes `ready`, so a status-keyed gate is unreachable.

    Every combination that could distinguish the two is present: an unprompted `draft`, an
    unprompted `ready`, and a prompted `draft`. A gate that required `ready` would let the first
    through; one that blocked anything not `ready` would refuse the third. Only reading the
    prompt gives this answer, which is why the two blank Shots block regardless of their status
    and the written one does not.
    """
    project = plan("", "", "A singer turns toward camera")
    project.shots[0].status = "draft"
    project.shots[1].status = "ready"
    project.shots[2].status = "draft"

    report = readiness_report(project)

    assert report.blocked_ids() == ["shot_0", "shot_1"]
    assert report.ready_count == 1


def test_identical_prompts_warn_and_the_plan_stays_submittable():
    report = readiness_report(plan("A singer turns toward camera", "A singer turns toward camera"))

    # The whole point of the split: sameness warns, and only emptiness blocks.
    assert report.ready is True
    assert report.blocking == []
    assert report.ready_count == 2
    assert [note.shot_ids for note in report.warnings] == [["shot_0", "shot_1"]]
    assert report.warnings[0].reason == SHOTS_SHARE_ONE_PROMPT


def test_case_and_spacing_only_differences_count_as_identical_not_as_merely_similar():
    """`"A Shot"` and `"a   shot"` are the same prompt, and the report has to say so.

    Reported as *identical* rather than as high overlap on purpose: a Director told two prompts
    are "similar" goes looking for the difference, and there is none to find.
    """
    report = readiness_report(plan("A Shot", "a   shot"))

    assert report.ready is True
    assert [note.reason for note in report.warnings] == [SHOTS_SHARE_ONE_PROMPT]
    assert report.warnings[0].shot_ids == ["shot_0", "shot_1"]


def test_a_punctuation_only_difference_is_reported_rather_than_missed_entirely():
    """`"A singer turns."` and `"A singer turns"` are the same shot by any reading.

    Splitting on whitespace leaves the full stop welded to the last word, so the pair scores
    0.75 -- under the threshold, and reported as nothing at all. Splitting on word boundaries
    scores it 1.0. It stays a *similarity* rather than an identity, because the frozen definition
    of identical is equality after lowercasing and whitespace collapse and a full stop survives
    both; calling it identical would quietly widen a definition this spec pinned.
    """
    report = readiness_report(plan("A singer turns.", "A singer turns"))

    assert report.ready is True
    assert [note.reason for note in report.warnings] == [SHOTS_LACK_VARIANCE]
    assert _overlap(_words("A singer turns."), _words("A singer turns")) == 1.0


def test_repeating_a_word_is_deliberately_not_variance():
    """Words are a set, so `"red red red door"` and `"red door"` are one prompt twice.

    Pinned because it is a decision, not an oversight: emphasis is not a different shot, and the
    alternative -- counting repetition -- calls a pair distinct on the strength of a stutter. The
    cost is stated here rather than discovered later: a prompt cannot make itself distinct by
    repeating a word.
    """
    report = readiness_report(plan("red red red door", "red door"))

    assert report.ready is True
    assert [note.reason for note in report.warnings] == [SHOTS_LACK_VARIANCE]
    assert _words("red red red door") == _words("red door") == {"red", "door"}


def test_a_blank_prompt_has_no_words_at_all_rather_than_one_empty_one():
    """Reached directly, because the report cannot show this: blocked Shots are never compared.

    `" ".join("".split()).split(" ")` yields `[""]`, so a blank prompt carried a phantom token
    that joined every intersection and union it took part in -- and made the divide-by-zero floor
    in `_overlap` unreachable. Both halves are pinned here because the only way this becomes
    observable through `readiness_report` is a change that also lets blank prompts through.
    """
    assert _words("") == set()
    assert _words("   \n\t") == set()
    assert _overlap(set(), set()) == 0.0
    assert _overlap(set(), {"door"}) == 0.0


def test_word_overlap_above_the_threshold_warns_without_blocking():
    report = readiness_report(plan(TEN_WORDS, f"{TEN_WORDS} slowly"))

    assert report.ready is True
    assert [note.reason for note in report.warnings] == [SHOTS_LACK_VARIANCE]
    assert report.warnings[0].shot_ids == ["shot_0", "shot_1"]
    # The pair shares 10 of the 11 words it spans; the rule is the threshold, not the example.
    assert 10 / 11 > NEAR_DUPLICATE_OVERLAP


def test_overlap_exactly_at_the_threshold_is_not_a_warning():
    """The one value that tells `>` from `>=`, and the only one either spelling disagrees on.

    Nine shared words out of the ten the pair spans is exactly 0.9. Without this the comparison
    could be loosened to `>=` and every other case here would still pass.
    """
    report = readiness_report(plan(NINE_WORDS, f"{NINE_WORDS} slowly"))

    assert 9 / 10 == NEAR_DUPLICATE_OVERLAP
    assert _overlap(_words(NINE_WORDS), _words(f"{NINE_WORDS} slowly")) == NEAR_DUPLICATE_OVERLAP
    assert report.ready is True
    assert report.warnings == []


def test_overlap_below_the_threshold_is_not_reported_at_all():
    """Eight shared words out of ten spanned is 0.8, and 0.8 is not a warning.

    Without this, a gate that warned on any shared word at all would pass every other case
    here -- and would flag every plan written in one visual language, which is every good plan.
    """
    report = readiness_report(plan(f"{EIGHT_WORDS} slowly", f"{EIGHT_WORDS} outward"))

    assert _overlap(_words(f"{EIGHT_WORDS} slowly"), _words(f"{EIGHT_WORDS} outward")) == 0.8
    assert report.ready is True
    assert report.warnings == []


def test_a_blocked_shot_is_never_also_reported_as_a_duplicate():
    """Two empty prompts are trivially identical, and saying so is noise on top of the block.

    The warning would also resolve itself the instant either block is acted on, so it can only
    ever bury the entries that have to be read.
    """
    report = readiness_report(plan("", "   ", "A singer turns toward camera"))

    assert report.blocked_ids() == ["shot_0", "shot_1"]
    assert report.warnings == []


def test_a_fully_prompted_varied_plan_is_ready_with_a_count_and_no_warnings():
    report = readiness_report(
        plan(
            "A singer turns toward camera in a sodium corridor",
            "A drone rises over the desert at dawn",
            "Hands push open a steel door into white light",
        )
    )

    assert report.ready is True
    assert report.ready_count == 3
    assert report.shot_count == 3
    assert report.blocking == []
    assert report.warnings == []


def test_an_empty_plan_is_not_ready_and_names_no_shots():
    report = readiness_report(Project(name="Empty"))

    assert report.ready is False
    assert report.shot_count == 0
    assert report.ready_count == 0
    # It says the plan is empty rather than naming a Shot, because there is no Shot to name.
    assert [note.reason for note in report.blocking] == [PLAN_WITHOUT_SHOTS]
    assert report.blocked_ids() == []
    assert report.blocked_labels() == []


def test_the_report_is_ordered_by_song_position_and_not_by_manifest_order():
    """Two calls over one project must produce one report, and it reads as the timeline reads."""
    project = plan("", "", "")
    project.shots = [project.shots[2], project.shots[0], project.shots[1]]

    report = readiness_report(project)

    assert report.blocked_ids() == ["shot_0", "shot_1", "shot_2"]
    assert readiness_report(project) == report


def test_a_shot_is_labelled_by_its_position_on_the_timeline_and_carries_its_id():
    """The clip is drawn `SHOT 01` from *manifest* position; the report is in song order.

    The two orderings differ here on purpose -- the manifest is shuffled -- so a label taken from
    the report's own ordering would point the Director at the wrong clip. The id rides along
    because the number changes when Shots are reordered and the id never does.
    """
    project = plan("", "", "")
    project.shots = [project.shots[2], project.shots[0], project.shots[1]]

    report = readiness_report(project)

    assert shot_label(project, project.shots[0]) == "SHOT 01 (shot_2)"
    # Song order for the notes, manifest numbering inside each label.
    assert report.blocked_ids() == ["shot_0", "shot_1", "shot_2"]
    assert report.blocked_labels() == [
        "SHOT 02 (shot_0)",
        "SHOT 03 (shot_1)",
        "SHOT 01 (shot_2)",
    ]
    # Labels line up with ids note by note, so a client can pair them without a lookup.
    assert all(len(note.labels) == len(note.shot_ids) for note in report.blocking)
    # A Shot that is not in this project claims no position rather than inventing one.
    assert shot_label(project, Shot(id="shot_elsewhere", start=0, duration=5)) == "shot_elsewhere"


def test_the_blocking_only_report_skips_the_pairwise_pass_and_says_that_it_did():
    """The submission route asks N times per batch, and sameness cannot change its answer.

    Computing the pairwise pass there runs an O(N^2) comparison N times over one batch and throws
    the result away each time. `warnings_computed` is reported so an empty `warnings` list from
    this mode can never be misread as "this plan has no duplicates" -- which is exactly what a
    caller would conclude from the field alone.
    """
    project = plan("A singer turns toward camera", "A singer turns toward camera", "")

    full = readiness_report(project)
    blocking_only = readiness_report(project, include_warnings=False)

    assert full.warnings_computed is True
    assert len(full.warnings) == 1
    assert blocking_only.warnings_computed is False
    assert blocking_only.warnings == []
    # Every answer the route actually uses is identical between the two modes.
    assert blocking_only.ready == full.ready is False
    assert blocking_only.blocked_ids() == full.blocked_ids() == ["shot_2"]
    assert blocking_only.ready_count == full.ready_count == 2

    # And on a plan that *is* ready, both ways round. Asserting only the blocked plan leaves
    # `ready` free to be tied to the mode -- the skipped pass could switch a submittable plan to
    # unsubmittable for every caller that skips it, which is every submission.
    submittable = plan("A singer turns toward camera", "A drone rises over the desert")
    assert readiness_report(submittable).ready is True
    assert readiness_report(submittable, include_warnings=False).ready is True


def test_variance_warnings_are_capped_and_the_overflow_is_counted_not_dropped():
    """Sameness is pairwise, so N identical prompts produce C(N,2) notes -- 20 produce 190.

    Capped because a report nobody can read is a report nobody reads, and counted rather than
    silently truncated because "12 warnings" and "12 warnings and 178 you cannot see" are
    different facts about the plan.
    """
    shots = VARIANCE_WARNING_LIMIT + 4
    report = readiness_report(plan(*["A singer turns toward camera"] * shots))

    pairs = shots * (shots - 1) // 2
    assert pairs > VARIANCE_WARNING_LIMIT
    assert len(report.warnings) == VARIANCE_WARNING_LIMIT
    assert report.warnings_omitted == pairs - VARIANCE_WARNING_LIMIT
    # Capping is presentation only: it never changes whether the plan may be submitted.
    assert report.ready is True


def test_readiness_is_derived_and_writes_nothing_back_onto_the_project():
    """AD-5: anything computable from the manifest is computed. A stored flag goes stale."""
    project = plan("A singer turns toward camera", "")
    before = project.model_dump(mode="json")

    readiness_report(project)

    assert project.model_dump(mode="json") == before
    assert not any(field.startswith("ready") for field in Shot.model_fields)


# --- A stale reference map, reported before the batch is confirmed (2026-08-21) -----------
#
# The gap this closes: `app.generate_h3` has refused a shot whose stored expansion carries a stale
# reference map since 2026-08-20, and this report said nothing about it. So the pre-flight told the
# Director "33 ready", they confirmed the GPU cost, the batch started, and *then* one shot was
# skipped by name. Every other thing this module reports -- the unprompted shot, the near-duplicate
# pair, the out-of-band window -- is surfaced before that confirmation precisely so it can still be
# acted on.
#
# It **blocks**, and `SHOT_WITH_STALE_REFERENCE_MAP` carries the argument. The tests below hold both
# halves of the consequence: `ready` goes false, and `ready_count` still counts the shot as having a
# prompt, because it has one.


def mapped(*, cites: int, expansion: str = "", stored_map: str = "") -> Project:
    """One shot citing `cites` of two library pictures, holding `expansion` and `stored_map`.

    Written straight onto the model rather than through a route, which is what lets one helper
    build **both** staleness shapes: the prose form carries its map in its own text, and a document
    expansion carries it in `h3_prompt_map`. `tests/test_api.py` drives the same states through the
    real routes and asserts the submit route agrees with this report.
    """
    project = plan("A singer turns toward camera")
    project.assets = [
        Asset(id="asset_bed", name="Dusk Warehouse Bed", kind="setting", path="assets/bed.png"),
        Asset(id="asset_lead", name="HarderFaster sheet", kind="character", path="assets/lead.png"),
    ]
    shot = project.shots[0]
    shot.citations = [
        AssetCitation(asset_id=asset.id, role="reference", order=index)
        for index, asset in enumerate(project.assets[:cites])
    ]
    shot.h3_prompt = expansion
    shot.h3_prompt_map = stored_map
    return project


BED_ONLY = reference_map_sentence(["<Picture 1> is Dusk Warehouse Bed"])
BED_AND_LEAD = reference_map_sentence(
    ["<Picture 1> is Dusk Warehouse Bed", "<Picture 2> is HarderFaster sheet"]
)


def test_a_prose_expansion_whose_citations_moved_blocks_by_name():
    """The Director's own project's shape: the prose form carries its map in its own first line.

    The live defect of 2026-08-20 was exactly this — a character sheet attached to a shot whose
    stored prose still named only the bed — and `app.reference_prompt` submits a stored expansion
    **alone**, so the render would have been conditioned on a map describing pictures the payload
    does not wire.
    """
    project = mapped(cites=2, expansion=f"{BED_ONLY} She turns toward camera.")

    report = readiness_report(project)

    assert report.ready is False
    assert report.blocked_ids() == ["shot_0"]
    assert report.blocked_ids(kind=NOTE_KIND_STALE_MAP) == ["shot_0"]
    # And not under the *other* block's kind, which is what keeps `generate_h3`'s prompt refusal
    # from being raised over a shot that has a prompt.
    assert report.blocked_ids(kind=NOTE_KIND_PROMPT) == []
    note = report.blocking[0]
    assert note.kind == NOTE_KIND_STALE_MAP
    assert note.reason == SHOT_WITH_STALE_REFERENCE_MAP
    assert note.labels == [shot_label(project, project.shots[0])]
    # It has a prompt, and the summary line that counts prompts must go on saying so. What this
    # shot loses is `ready` and a line in `blocking`, not its place in the count.
    assert report.ready_count == 1
    assert report.shot_count == 1


def test_a_prose_expansion_that_still_names_the_references_it_cites_is_silent():
    """The fresh case, and the Director's live project: 33 prose shots, nothing to report."""
    project = mapped(cites=1, expansion=f"{BED_ONLY} She turns toward camera.")

    report = readiness_report(project)

    assert report.ready is True
    assert report.blocking == []
    assert report.window_warnings == []


def test_a_document_expansion_blocks_when_the_map_recorded_beside_it_no_longer_matches():
    """The second shape. A document expansion never writes the map into its own text -- the
    specialist weaves `<Picture 2>` into prose -- so `Shot.h3_prompt_map` is the only thing that
    can tell a stale one from a fresh one, and it is what this arm compares.
    """
    project = mapped(
        cites=2,
        expansion="integrated_multimodal_description: [Shot 1] A grey wolf paces.",
        stored_map=BED_ONLY,
    )

    report = readiness_report(project)

    assert report.ready is False
    assert report.blocking[0].kind == NOTE_KIND_STALE_MAP
    assert report.blocking[0].reason == SHOT_WITH_STALE_REFERENCE_MAP

    # The same expansion with the map it was actually written against reports nothing.
    fresh = mapped(
        cites=2,
        expansion="integrated_multimodal_description: [Shot 1] A grey wolf paces.",
        stored_map=BED_AND_LEAD,
    )
    assert readiness_report(fresh).ready is True
    assert readiness_report(fresh).blocking == []


def test_a_shot_with_no_expansion_is_never_reported_stale():
    """It has not been expanded; there is nothing for a map to be stale against.

    Asserted with a *stale stored map still on the shot*, which is the state that would fool a
    check keyed on `h3_prompt_map` alone: a document expansion cleared out of the inspector leaves
    the record behind, and reporting that shot would tell a Director to fix an expansion that is
    not there. A blank `h3_prompt` is the submit fallback, and the map it builds is correct by
    construction.
    """
    project = mapped(cites=2, expansion="", stored_map=BED_ONLY)

    report = readiness_report(project)

    assert report.ready is True
    assert report.blocking == []
    assert not stale_reference_map(project, project.shots[0])


def test_reporting_a_stale_map_writes_nothing_back_onto_the_project():
    """AD-5 again, for the note that is easiest to be tempted to cache.

    Staleness is a comparison against a sentence rebuilt from the citations, so a stored answer
    would be wrong the moment an asset was attached -- which is the only way a shot ever becomes
    stale in the first place.
    """
    project = mapped(cites=2, expansion=f"{BED_ONLY} She turns toward camera.")
    before = project.model_dump(mode="json")

    assert readiness_report(project).ready is False

    assert project.model_dump(mode="json") == before
    assert not any(field.startswith("stale") for field in Shot.model_fields)


def test_readiness_and_the_submit_route_share_one_staleness_implementation():
    """Not two functions that agree today: the same object, reached from both modules.

    The reference *numbering* existed twice in this codebase and the two disagreed the moment a
    video was cited (2026-08-20). This is the same rule applied to the same kind of comparison, and
    an identity check is the only assertion that cannot be satisfied by a second copy that happens
    to be right about the cases someone thought of.
    """
    from music_video_producer import app, batch, reference_map

    assert batch.stale_reference_map is reference_map.stale_reference_map
    assert app.stale_reference_map is reference_map.stale_reference_map
    # And no module holds a second definition of it.
    definitions = [
        path
        for path in Path("src/music_video_producer").glob("*.py")
        if "def stale_reference_map" in path.read_text(encoding="utf-8")
    ]
    assert [path.name for path in definitions] == ["reference_map.py"]


def test_the_pre_flight_note_and_the_submit_refusal_are_one_explanation():
    """One problem, one Director, one set of words -- `api.js`'s `READINESS_REMEDY` rule.

    The remedy in particular is why: it names clearing the H3 box *first* because that is the fix
    that always works, and the re-expand route it names second needs a `mark-draft` step that a
    second wording would be free to forget. Asserted as substring identity against the shared
    clauses, so neither sentence can be reworded on its own.
    """
    from music_video_producer.app import STALE_REFERENCE_MAP_REFUSAL

    for clause in (
        STALE_REFERENCE_MAP_CAUSE,
        STALE_REFERENCE_MAP_CONSEQUENCE,
        STALE_REFERENCE_MAP_REMEDY,
    ):
        assert clause in STALE_REFERENCE_MAP_REFUSAL
        assert clause in SHOT_WITH_STALE_REFERENCE_MAP
    assert "send the shot back to draft and expand it again" in STALE_REFERENCE_MAP_REMEDY


# --- The shot-length band, reported before the Director discovers it (2026-08-20) ---------
#
# Until this, a window outside 4-15 s reached exactly one surface: a `warnings` string from
# `timeline.build_director_timeline`, printed only by the Compile dry run. The Director's own
# project carried five hand-timed sub-4-second windows and this report said `ready: True` and
# nothing else about any of them.


def test_a_short_window_warns_with_its_own_render_numbers_and_still_reports_ready():
    """Short is legitimate music-video editing now, so it warns and never blocks.

    The note carries this shot's own frame count and buffer, because "it renders anyway" is a
    claim a Director should be able to check against the take that arrives.
    """
    project = plan("A singer turns toward camera")
    project.shots[0].duration = 2.083

    report = readiness_report(project)

    assert report.ready is True
    assert report.blocking == []
    assert [note.shot_ids for note in report.window_warnings] == [["shot_0"]]
    note = report.window_warnings[0]
    assert note.kind == NOTE_KIND_WINDOW_SHORT
    assert note.reason == SHOT_WINDOW_BELOW_BAND.format(
        duration=2.083, minimum=4.0, frames=107, rendered=107 / 24, buffer=107 / 24 - 2.083
    )
    assert "107 frames" in note.reason and "does not block submission" in note.reason


def test_a_long_window_warns_and_says_the_extender_is_out_of_reach():
    """The Director's ruling: "when dragging a clip past that it should turn yellow but we
    arent dead yet". A warning state, not a refusal — and the intended fix they name, the
    video extender, is built as a payload builder and reachable from nowhere, so the sentence
    must claim neither that it works nor that it does not exist.
    """
    project = plan("A singer turns toward camera")
    project.shots[0].duration = 20.0

    report = readiness_report(project)

    assert report.ready is True
    assert report.blocking == []
    note = report.window_warnings[0]
    assert note.kind == NOTE_KIND_WINDOW_LONG
    assert note.reason == SHOT_WINDOW_ABOVE_BAND.format(duration=20.0, maximum=15.0)
    assert "not reachable" in note.reason and "does not block submission" in note.reason
    # Both halves of that claim are true of this repository, not merely written in it: the
    # builder exists, and nothing calls it.
    assert "def build_ltx25_extend_payload" in Path(
        "src/music_video_producer/workflows.py"
    ).read_text(encoding="utf-8")
    assert "build_ltx25_extend_payload" not in Path(
        "src/music_video_producer/app.py"
    ).read_text(encoding="utf-8")
    assert SHOT_MODE_SPECS["extend"].adapter == ""


def test_a_window_inside_the_band_says_nothing_at_all():
    """Both ends, and the two boundaries themselves, which are inclusive."""
    project = plan("A singer turns toward camera")
    for duration in (4.0, 5.0, 15.0):
        project.shots[0].duration = duration
        assert readiness_report(project).window_warnings == [], duration
        assert window_band_note(project.shots[0]) == ("", ""), duration
    # And just outside each of them, so the comparison is asserted at the edge rather than in
    # the middle where any threshold would pass.
    for duration, kind in ((3.999, NOTE_KIND_WINDOW_SHORT), (15.001, NOTE_KIND_WINDOW_LONG)):
        project.shots[0].duration = duration
        assert window_band_note(project.shots[0])[0] == kind, duration


def test_the_band_is_reported_for_a_blocked_shot_too_and_never_moves_ready():
    """A window is a fact about one shot, unlike sameness, so a blocked shot's is just as
    true — and hiding it would make the note appear only after the prompt was written, which
    is the late discovery this exists to end."""
    project = plan("", "A singer turns toward camera")
    project.shots[0].duration = 2.0
    project.shots[1].duration = 20.0

    report = readiness_report(project)

    assert report.ready is False  # the empty prompt, and only the empty prompt
    assert [note.reason for note in report.blocking] == [SHOT_WITHOUT_PROMPT]
    assert [note.kind for note in report.window_warnings] == [
        NOTE_KIND_WINDOW_SHORT,
        NOTE_KIND_WINDOW_LONG,
    ]
    assert [note.shot_ids for note in report.window_warnings] == [["shot_0"], ["shot_1"]]


def test_window_notes_stay_out_of_the_sameness_list_that_the_browser_labels():
    """`api.js` labels every note in `warnings` "Near-duplicate" and counts the list as pairs,
    so a window note posted there would reach the Director under a name that is not what it
    says. One list, one kind of note — and the kinds are values, so a clip can colour from
    them without matching on the sentence."""
    project = plan("A singer turns toward camera", "A singer turns toward camera")
    project.shots[0].duration = 2.0

    report = readiness_report(project)

    assert [note.kind for note in report.warnings] == [NOTE_KIND_SAMENESS]
    assert [note.kind for note in report.window_warnings] == [NOTE_KIND_WINDOW_SHORT]
    assert all(len(note.shot_ids) == 2 for note in report.warnings)
    # The blocking half carries its kind too, so no note anywhere is uncategorised.
    assert readiness_report(plan("")).blocking[0].kind == NOTE_KIND_PROMPT
    assert readiness_report(Project(name="Empty")).blocking[0].kind == NOTE_KIND_PROMPT


def test_the_band_is_reported_even_when_the_pairwise_pass_is_skipped():
    """`include_warnings=False` is the submission route's hot path and skips *sameness*, which
    is pairwise. The band is per-shot and costs one comparison, so there is nothing to save and
    the Director's own project would otherwise be told about its windows only on some calls."""
    project = plan("A singer turns toward camera")
    project.shots[0].duration = 2.0

    report = readiness_report(project, include_warnings=False)

    assert report.warnings_computed is False and report.warnings == []
    assert [note.kind for note in report.window_warnings] == [NOTE_KIND_WINDOW_SHORT]


# --- The window against its own take (2026-08-21) -----------------------------------------
#
# The Director's second yellow: "if the bounds of the shots window are dragged beyond where that
# clip covers then the shot would turn yellow to warn that the bounds was gone past. The user
# could then readjust or regenerate to fit the newly wanted shot timeframe. This should help
# prevent lipsync clips from being dragged away from where it actually matches up with the music."
#
# It warns and never blocks, on the band's terms exactly, and it is answered only for a take that
# recorded its own window — a take without one cannot be checked by anything on the manifest.


def rendered(duration: float = 5.0, *, lead: float = 0.25, nudge: float = 0.0) -> Shot:
    """One shot with a take that recorded its window, as the H3 route writes it at submission."""
    return Shot(
        id="shot_0",
        start=10.0,
        duration=duration,
        prompt="A singer turns toward camera",
        latest_output="shots/shot_0-h3_00001.mp4",
        latest_take_lead=lead,
        latest_take_start=10.0,
        latest_take_duration=5.0,
        trim_nudge=nudge,
    )


def test_a_window_still_inside_its_take_says_nothing():
    """The take is `over_render_frames(5.0) / 24` = 5.875 s of picture and the cut sits 0.25 s in,
    so a 5 s window has 0.625 s of tail to spare. Nothing is reported, at rest or nudged inside
    the buffer."""
    assert take_coverage_note(rendered()) == ("", "")
    assert take_coverage_note(rendered(nudge=0.5)) == ("", "")
    assert take_coverage_note(rendered(nudge=-0.25)) == ("", "")


def test_a_window_dragged_off_the_end_of_its_take_warns_with_every_number():
    """The overrun end. `needed` and `take` are both named because the Director's two fixes —
    readjust, or regenerate — need to know by how much and against what."""
    shot = rendered(nudge=1.0)

    kind, reason = take_coverage_note(shot)

    # `over_render_frames(5.0)` is 141 frames: 5.5 s of margin snapped up the 17k+5 grid.
    take = 141 / 24
    assert kind == NOTE_KIND_TAKE_UNCOVERED
    assert reason == SHOT_WINDOW_PAST_TAKE.format(
        past=1.25 + 5.0 - take, take=take, needed=1.25 + 5.0, offset=1.25, duration=5.0
    )
    assert "does not block submission" in reason
    # And it is a warning: nothing about `ready` moves, exactly as the band does not move it.
    project = Project(name="Coverage", shots=[shot])
    report = readiness_report(project)
    assert report.ready is True
    assert report.blocking == []
    assert [note.kind for note in report.window_warnings] == [NOTE_KIND_TAKE_UNCOVERED]
    assert report.window_warnings[0].shot_ids == ["shot_0"]
    assert report.window_warnings[0].labels == [shot_label(project, shot)]


def test_a_window_dragged_back_before_its_take_begins_warns_from_the_other_end():
    """Reachable only because the locked move-drag compensates `trim_nudge` and is deliberately
    **not** clamped — the Director's ruling is that this colours and never constrains. The nudge
    buttons still floor at the recorded lead; a drag does not."""
    kind, reason = take_coverage_note(rendered(nudge=-1.0))

    assert kind == NOTE_KIND_TAKE_UNCOVERED
    assert reason == SHOT_WINDOW_BEFORE_TAKE.format(behind=0.75)
    assert "does not block submission" in reason


def test_the_coverage_check_is_asserted_at_its_own_edges_and_not_in_the_middle():
    """Half a frame of tolerance each way, because both sides are manifest floats and one is a
    division. A threshold asserted in the middle of its range passes for any threshold."""
    take = 141 / 24  # 5.875 s of picture for a 5 s window
    # The exact fit: the window ends on the take's last frame.
    assert take_coverage_note(rendered(nudge=take - 5.0 - 0.25)) == ("", "")
    # Inside the tolerance, and just outside it.
    assert take_coverage_note(
        rendered(nudge=take - 5.0 - 0.25 + TAKE_COVERAGE_TOLERANCE_SECONDS / 2)
    ) == ("", "")
    assert take_coverage_note(
        rendered(nudge=take - 5.0 - 0.25 + TAKE_COVERAGE_TOLERANCE_SECONDS * 2)
    )[0] == NOTE_KIND_TAKE_UNCOVERED
    # The same pair at the front edge, where the offset reaches zero.
    assert take_coverage_note(rendered(nudge=-0.25)) == ("", "")
    assert take_coverage_note(
        rendered(nudge=-0.25 - TAKE_COVERAGE_TOLERANCE_SECONDS / 2)
    ) == ("", "")
    assert take_coverage_note(
        rendered(nudge=-0.25 - TAKE_COVERAGE_TOLERANCE_SECONDS * 2)
    )[0] == NOTE_KIND_TAKE_UNCOVERED


def test_a_take_that_never_recorded_a_window_is_never_warned_about():
    """`latest_take_duration` of 0 is "never snapshotted": every take rendered before 2026-08-21,
    including the 33 in the Director's own project, and every hand-picked clip, whose bookkeeping
    `select_shot_clip` clears. The only window such a take has on the manifest is the *live* one,
    which is a fact about the plan rather than about the file — checking against it would report a
    shot as uncovered precisely because it had been edited. Silence, never a guess."""
    legacy = rendered(nudge=9.0)
    legacy.latest_take_duration = 0.0

    assert take_coverage_note(legacy) == ("", "")
    assert readiness_report(Project(name="Legacy", shots=[legacy])).window_warnings == []
    # And a shot with no take at all, which is every shot before its first render.
    unrendered = rendered(nudge=9.0)
    unrendered.latest_output = ""
    assert take_coverage_note(unrendered) == ("", "")


def test_a_long_window_over_a_take_it_has_outgrown_reports_both_facts():
    """Two notes for one shot, which is why nothing server-side keys this list by shot id. The
    band's note comes first — it is decided first — and the client's own precedence picks which
    one the clip's single accessible name carries."""
    shot = rendered(duration=20.0)

    report = readiness_report(Project(name="Both", shots=[shot]))

    assert [note.kind for note in report.window_warnings] == [
        NOTE_KIND_WINDOW_LONG,
        NOTE_KIND_TAKE_UNCOVERED,
    ]
    assert [note.shot_ids for note in report.window_warnings] == [["shot_0"], ["shot_0"]]
    assert report.ready is True


def test_the_coverage_note_is_reported_when_the_pairwise_pass_is_skipped_too():
    """The submission route's hot path skips sameness and keeps every window note, for the band's
    reason: this is per-shot and costs one comparison."""
    report = readiness_report(
        Project(name="Coverage", shots=[rendered(nudge=1.0)]), include_warnings=False
    )

    assert report.warnings_computed is False
    assert [note.kind for note in report.window_warnings] == [NOTE_KIND_TAKE_UNCOVERED]


def test_the_coverage_check_writes_nothing_back_onto_the_shot():
    """AD-5's rule, asserted for the new note as it is for the report as a whole: derived, never
    persisted. A stored coverage flag would go stale on the very next drag."""
    project = Project(name="Coverage", shots=[rendered(nudge=1.0)])
    before = project.model_dump(mode="json")

    readiness_report(project)

    assert project.model_dump(mode="json") == before


def test_timeline_does_not_import_batch_so_the_window_math_stays_a_pure_leaf():
    """The dependency runs one way: `batch` may read `timeline`, never the reverse.

    Asserted rather than assumed, because the reverse import would not fail — Python would
    resolve it — it would just make the two modules mutually dependent, and the next thing
    `batch` needs (submission ordering, batch orchestration, ComfyUI reconciliation, all of
    AD-5's remit) would drag that weight into the one module that is pure window math today.
    """
    timeline_source = Path("src/music_video_producer/timeline.py").read_text(encoding="utf-8")

    assert "batch" not in timeline_source
    # And the intended direction really is in place, so this is not passing by both being absent.
    batch_source = Path("src/music_video_producer/batch.py").read_text(encoding="utf-8")
    assert "from .timeline import" in batch_source


def test_the_refusal_names_every_blocking_shot_from_one_template():
    """One sentence for one rule, whether one Shot blocks or four do."""
    assert readiness_refusal(["shot_1"]) == READINESS_REFUSAL.format(shots="shot_1")
    assert "shot_1, shot_4" in readiness_refusal(["shot_1", "shot_4"])
    # It says what was *not* done, because a refusal that does not is read as a failure.
    assert "nothing was sent to ComfyUI" in readiness_refusal(["shot_1"])


def test_the_refusal_stops_listing_and_starts_counting_past_the_name_limit():
    """A twenty-Shot batch refusal is a toast, not a report; an unbounded list is unreadable."""
    names = [f"SHOT {index:02d}" for index in range(1, REFUSAL_NAME_LIMIT + 4)]

    refusal = readiness_refusal(names)

    assert ", ".join(names[:REFUSAL_NAME_LIMIT]) in refusal
    assert "and 3 more" in refusal
    assert names[REFUSAL_NAME_LIMIT] not in refusal
    # Exactly at the limit nothing is elided, and nothing claims it was.
    assert "more" not in readiness_refusal(names[:REFUSAL_NAME_LIMIT])


def test_the_refusal_of_an_empty_plan_says_the_plan_is_empty():
    """`[]` is a designed-for input, not a caller error.

    The empty-plan note carries no ids by design, so every id extractor returns `[]` for it and
    any caller that pipes one into the refusal gets here. "Not submitted: no prompt on ." is
    worse than useless -- it names a Shot-shaped hole and says nothing true.
    """
    assert readiness_refusal([]) == PLAN_WITHOUT_SHOTS
    assert "no prompt on ." not in readiness_refusal([])


# --------------------------------------------------------------------------------------------
# Render reconciliation -- AD-1's tick, against a scripted ComfyUI that counts every question.
# --------------------------------------------------------------------------------------------


class ScriptedComfy:
    """A ComfyUI double that records exactly what the reconciler asked it.

    `queue_payload` is the real ``/queue`` shape: two buckets of ``[number, prompt_id, ...]``
    lists. `histories` maps a prompt id to a `HistoryResult`; an unlisted id answers as
    ComfyUI answers for a prompt it has never finished -- an empty history, status "queued".
    Setting `queue_error` fails the queue call the way a dead server does.
    """

    def __init__(self, running=(), pending=(), histories=None):
        self.queue_payload = {
            "queue_running": [[1, prompt_id, {}] for prompt_id in running],
            "queue_pending": [[2, prompt_id, {}] for prompt_id in pending],
        }
        self.histories = histories or {}
        self.queue_error = False
        self.history_errors: set[str] = set()
        self.queue_calls = 0
        self.history_calls: list[str] = []

    async def queue(self):
        self.queue_calls += 1
        if self.queue_error:
            raise ComfyError("Cannot reach ComfyUI")
        return self.queue_payload

    async def history(self, prompt_id):
        self.history_calls.append(prompt_id)
        if prompt_id in self.history_errors:
            raise ComfyError("history unavailable")
        return self.histories.get(prompt_id) or HistoryResult(
            prompt_id=prompt_id, status="queued", known=False
        )


def render_plan() -> Project:
    """A project mid-render: one open job per kind of surface a completion reaches."""
    project = Project(name="Reconcile")
    project.assets = [
        Asset(
            id="asset_flux",
            name="Lead singer",
            kind="character",
            path="",
            source="flux",
            prompt_id="prompt-flux",
        ),
    ]
    project.shots = [
        Shot(
            id="shot_h3",
            start=0,
            duration=5,
            prompt="A singer turns",
            status="queued",
            prompt_id="prompt-h3",
        ),
    ]
    project.song = Song(title="Spine", source="generated", path="", prompt_id="prompt-music")
    project.jobs = [
        RenderJob(
            id="job_flux",
            kind="flux",
            status="queued",
            prompt_id="prompt-flux",
            target_id="asset_flux",
        ),
        RenderJob(
            id="job_h3", kind="h3", status="queued", prompt_id="prompt-h3", target_id="shot_h3"
        ),
        RenderJob(
            id="job_music",
            kind="music",
            status="queued",
            prompt_id="prompt-music",
            target_id="song",
        ),
        RenderJob(
            id="job_done",
            kind="flux",
            status="complete",
            prompt_id="prompt-done",
            target_id="asset_gone",
        ),
    ]
    return project


def completed(prompt_id: str, filename: str) -> HistoryResult:
    return HistoryResult(
        prompt_id=prompt_id,
        status="complete",
        outputs=[{"subfolder": "music-video-producer\\p\\out", "filename": filename}],
    )


async def test_an_idle_project_asks_comfyui_nothing_at_all():
    """The zero-request half of AD-1: no open job, no `/queue`, no `/history`, no anything."""
    project = Project(name="Idle")
    project.jobs = [
        RenderJob(kind="flux", status="complete", prompt_id="prompt-1", target_id="a"),
        # Non-terminal but with no prompt id: nothing to look up, so nothing may be asked.
        RenderJob(kind="post", status="queued", prompt_id="", target_id="b"),
    ]
    comfy = ScriptedComfy()

    outcome = await reconcile_render_jobs(project, comfy)

    assert outcome.changed is False
    assert outcome.comfy_online is True
    assert comfy.queue_calls == 0
    assert comfy.history_calls == []
    assert reconcilable_jobs(project) == []


async def test_one_tick_fetches_the_queue_once_and_history_only_for_absent_jobs():
    """The fan-out AD-1 forbids: N open jobs used to be N `/queue` reads of one answer."""
    project = render_plan()
    comfy = ScriptedComfy(
        running=("prompt-h3",),
        pending=("prompt-music",),
        histories={"prompt-flux": completed("prompt-flux", "singer_00001_.png")},
    )

    outcome = await reconcile_render_jobs(project, comfy)

    assert comfy.queue_calls == 1
    # Only the job the queue no longer holds is asked about; the two still in it are settled
    # from the queue answer alone, and the already-terminal job is never asked about at all.
    assert comfy.history_calls == ["prompt-flux"]
    assert outcome.changed is True
    statuses = {job.id: job.status for job in project.jobs}
    assert statuses == {
        "job_flux": "complete",
        "job_h3": "running",
        "job_music": "queued",
        "job_done": "complete",
    }
    # The completion reached the thing the job was producing, through the shared adoption.
    assert project.assets[0].path == "music-video-producer/p/out/singer_00001_.png"


async def test_a_dead_comfyui_degrades_the_tick_instead_of_raising():
    """The poll endpoint answers every two seconds; a restart must not become an error spray."""
    project = render_plan()
    before = [job.status for job in project.jobs]
    comfy = ScriptedComfy()
    comfy.queue_error = True

    outcome = await reconcile_render_jobs(project, comfy)

    assert outcome.comfy_online is False
    assert outcome.changed is False
    assert [job.status for job in project.jobs] == before
    assert comfy.history_calls == []


async def test_one_failed_history_lookup_skips_that_job_and_reconciles_the_rest():
    project = render_plan()
    comfy = ScriptedComfy(
        histories={
            "prompt-flux": completed("prompt-flux", "singer_00001_.png"),
            "prompt-music": completed("prompt-music", "spine_00001_.flac"),
        },
    )
    comfy.history_errors = {"prompt-h3"}

    outcome = await reconcile_render_jobs(project, comfy)

    assert outcome.comfy_online is True
    assert outcome.changed is True
    statuses = {job.id: job.status for job in project.jobs}
    # The failed lookup left its job exactly as it was, for the next tick to ask again.
    assert statuses["job_h3"] == "queued"
    assert statuses["job_flux"] == "complete"
    assert statuses["job_music"] == "complete"
    assert project.song.path == "music-video-producer/p/out/spine_00001_.flac"


async def test_a_vanished_prompt_keeps_its_status_until_absence_persists():
    """Absent from the queue with an empty history is "nothing known yet" — for a while.

    One absent tick is what the seconds of a ComfyUI restart look like, so the early ticks
    only *count* (persisted, so the counter survives the browser's two-second poll). At
    `MISSING_TICKS_LIMIT` the absence is the answer: the prompt died with the queue, the job
    settles as that error in `JOB_LOST_WITH_QUEUE`'s words, and the h3 job's shot moves to
    `error` so it stops pinning render-status "active" and can be re-opened. Met three times
    live on 2026-08-19/20: pulled queue entries and a CUDA-crash restart each left jobs
    "queued" forever.
    """
    project = render_plan()
    comfy = ScriptedComfy()

    for tick in range(1, MISSING_TICKS_LIMIT):
        outcome = await reconcile_render_jobs(project, comfy)
        # The counter moved (and must be saved), but no status was invented.
        assert outcome.changed is True
        assert {job.status for job in project.jobs} == {"queued", "complete"}
    assert sorted(set(comfy.history_calls)) == ["prompt-flux", "prompt-h3", "prompt-music"]

    outcome = await reconcile_render_jobs(project, comfy)

    assert outcome.changed is True
    settled = {job.id: job for job in project.jobs if job.status != "complete"}
    assert {job.status for job in settled.values()} == {"error"}
    assert all(job.error == JOB_LOST_WITH_QUEUE for job in settled.values())
    # The h3 job's shot is released from its stuck in-flight status...
    assert project.shots[0].status == "error"
    # ...and a lost job that reappears in the queue is forgiven its strikes.
    reborn = ScriptedComfy(pending=["prompt-h3"])
    fresh = render_plan()
    fresh.jobs[1].missing_ticks = MISSING_TICKS_LIMIT - 1
    await reconcile_render_jobs(fresh, reborn)
    assert fresh.jobs[1].missing_ticks == 0
    assert fresh.jobs[1].status == "queued"


# --- The record-first ordering: what the pending window costs, and who heals it ----------


def pending_submission_plan() -> Project:
    """One shot and the record saved for it, in the window before the graph was accepted.

    Exactly what `generate_h3` writes on the near side of the Director's 2026-08-21 ordering,
    and exactly what a crash between the save and the submit would leave behind: a job whose
    `prompt_id` is the sentinel, and a Shot the acceptance never got to touch.
    """
    return Project(
        name="Pending",
        shots=[Shot(id="shot_a", start=0, duration=5, prompt="A corridor", status="ready")],
        jobs=[
            RenderJob(
                id="job_pending",
                kind="h3",
                status="queued",
                prompt_id=PENDING_SUBMISSION_PROMPT_ID,
                target_id="shot_a",
            )
        ],
    )


async def test_an_orphaned_pre_submit_record_settles_and_says_it_was_never_submitted():
    """The Director's ruling assumed reconciliation heals this. It does — verified here.

    The accepted cost of saving the record before submitting is that a crash in between leaves
    a record for a graph ComfyUI never heard of. The claim was that the existing machinery
    already covers it, and it does, with no new rule: the sentinel is a non-empty prompt id, so
    `reconcilable_jobs` counts the record and the poll keeps asking; ComfyUI's queue never holds
    that string and its history answers `known=False` for it; and `MISSING_TICKS_LIMIT` unknown
    ticks settle it exactly as they settle a prompt that died with the queue — job terminal,
    the shot released from its in-flight status, nothing left pinning render-status "active".

    What the sentinel changes is only the sentence. `JOB_LOST_WITH_QUEUE` says ComfyUI "no
    longer knows this prompt", which was never true of a record whose graph was never sent, so
    this one settles in `JOB_NEVER_SUBMITTED`'s words instead.
    """
    project = pending_submission_plan()
    comfy = ScriptedComfy()

    for _ in range(MISSING_TICKS_LIMIT - 1):
        outcome = await reconcile_render_jobs(project, comfy)
        assert outcome.changed is True
        # Counted, never invented: an absent tick is also what a ComfyUI restart looks like.
        assert project.jobs[0].status == "queued"
    # The lookup really is the ordinary one — no branch anywhere exempts the sentinel from it.
    assert comfy.history_calls == [PENDING_SUBMISSION_PROMPT_ID] * (MISSING_TICKS_LIMIT - 1)

    outcome = await reconcile_render_jobs(project, comfy)

    assert outcome.changed is True
    assert project.jobs[0].status == "error"
    assert project.jobs[0].error == JOB_NEVER_SUBMITTED
    assert project.jobs[0].error != JOB_LOST_WITH_QUEUE
    # Settled means settled: nothing polls for it and nothing counts the project busy.
    assert reconcilable_jobs(project) == []


async def test_a_record_whose_prompt_id_is_real_still_settles_in_the_queue_s_words():
    """The other side of that branch, so the new sentence cannot quietly take both cases."""
    project = pending_submission_plan()
    project.jobs[0].prompt_id = "prompt-real"

    for _ in range(MISSING_TICKS_LIMIT):
        await reconcile_render_jobs(project, ScriptedComfy())

    assert project.jobs[0].error == JOB_LOST_WITH_QUEUE


async def test_accept_submission_reopens_a_record_the_reconciler_settled_mid_submission():
    """The window is wider than the settle, so the acceptance has to be able to undo it.

    The eject before a submission is allowed twenty seconds and the `/prompt` call thirty;
    three ticks of the browser's two-second poll is six. So a slow-but-successful submission
    can find its own record already settled as never submitted — a verdict that was right on
    the evidence and is wrong the moment ComfyUI answers with a prompt id. A record left
    `error` carrying a live prompt id is never reconciled again, which is precisely the
    orphaned take this ordering exists to prevent.
    """
    project = pending_submission_plan()
    for _ in range(MISSING_TICKS_LIMIT):
        await reconcile_render_jobs(project, ScriptedComfy())
    assert project.jobs[0].status == "error"

    accept_submission(project.jobs[0], "prompt-real")

    assert project.jobs[0].prompt_id == "prompt-real"
    assert project.jobs[0].status == "queued"
    assert project.jobs[0].error == ""
    assert project.jobs[0].missing_ticks == 0
    # And it is being watched again, by the id ComfyUI actually answers for.
    assert reconcilable_jobs(project) == [project.jobs[0]]


def test_the_pending_marker_is_not_the_local_work_marker():
    """The one collision the sentinel had to avoid, asserted rather than assumed.

    An empty `prompt_id` means "local ffmpeg work" to `heal_orphaned_local_jobs`, to the
    assemble route's busy check and to `api.js`'s assembly-progress branch. A pending `h3` or
    `post` record carrying an empty id would be read as an assembly and healed to an assembly
    error at the next startup, while `reconcilable_jobs` — which is what settles it — would
    never look at it at all.
    """
    assert PENDING_SUBMISSION_PROMPT_ID
    # Not a UUID shape either, so it can never be confused for one ComfyUI minted.
    assert "-" in PENDING_SUBMISSION_PROMPT_ID and len(PENDING_SUBMISSION_PROMPT_ID) < 32
    project = pending_submission_plan()
    assert reconcilable_jobs(project) == [project.jobs[0]]
    project.jobs[0].prompt_id = ""
    assert reconcilable_jobs(project) == []


async def test_h3_completion_moves_the_shot_and_displaces_the_stale_review():
    project = render_plan()
    project.shots[0].latest_output = "old/take_00001.mp4"
    project.shots[0].latest_review = VisionInspectionRecord(summary="the previous take")
    comfy = ScriptedComfy(histories={"prompt-h3": completed("prompt-h3", "take_00002.mp4")})

    await reconcile_render_jobs(project, comfy)

    shot = project.shots[0]
    assert shot.status == "complete"
    assert shot.latest_output == "music-video-producer/p/out/take_00002.mp4"
    assert shot.latest_review is None


async def test_an_h3_execution_error_marks_the_shot_and_carries_the_reason():
    project = render_plan()
    comfy = ScriptedComfy(
        histories={
            "prompt-h3": HistoryResult(
                prompt_id="prompt-h3", status="error", error="KSampler: out of memory"
            )
        }
    )

    outcome = await reconcile_render_jobs(project, comfy)

    assert outcome.changed is True
    assert project.shots[0].status == "error"
    job = next(job for job in project.jobs if job.id == "job_h3")
    assert job.status == "error"
    assert job.error == "KSampler: out of memory"


async def test_music_adoption_is_keyed_by_prompt_id_not_by_whatever_song_is_loaded():
    """`apply_job_history` through the reconciler keeps the guard the per-job route had."""
    project = render_plan()
    project.song.prompt_id = "a-different-song"
    comfy = ScriptedComfy(
        histories={"prompt-music": completed("prompt-music", "orphan_00001_.flac")}
    )

    await reconcile_render_jobs(project, comfy)

    # The output is not lost -- it stays on the job -- but the loaded Song is untouched.
    assert project.song.path == ""
    job = next(job for job in project.jobs if job.id == "job_music")
    assert job.status == "complete"
    assert job.output_files == ["music-video-producer/p/out/orphan_00001_.flac"]


def test_queue_locations_reads_both_buckets_of_the_real_queue_shape():
    located = queue_locations(
        {
            "queue_running": [[0, "prompt-a", {"nodes": {}}]],
            "queue_pending": [[1, "prompt-b"], [2, "prompt-c"], "not-a-list"],
        }
    )

    assert located == {"prompt-a": "running", "prompt-b": "queued", "prompt-c": "queued"}
    assert queue_locations({}) == {}


def test_terminal_statuses_are_the_complement_of_what_reconciliation_touches():
    """The set the browser mirrors. Every JobStatus is on exactly one side of it."""
    assert TERMINAL_JOB_STATUSES == {"complete", "error", "cancelled"}
    assert set(get_args(JobStatus)) - TERMINAL_JOB_STATUSES == {"queued", "running"}


def test_the_status_report_is_the_fixed_shape_and_carries_no_editable_field():
    """AD-1's poll answer: jobs plus derived states, and nothing the inspector edits.

    The browser patches this straight onto the project the Director is typing into, so a field
    like `prompt` appearing here is the poll acquiring the means to overwrite typing with a
    two-second-old snapshot. The field lists are asserted exactly for that reason.
    """
    project = render_plan()
    report = render_status_report(project, comfy_online=False)

    assert report.active is True
    assert report.comfy_online is False
    assert [job.id for job in report.jobs] == [job.id for job in project.jobs]
    assert set(ShotRenderState.model_fields) == {"shot_id", "status", "latest_output"}
    assert set(AssetRenderState.model_fields) == {"asset_id", "path"}
    assert set(SongRenderState.model_fields) == {"path", "prompt_id"}
    assert report.shots[0].shot_id == "shot_h3"
    assert report.assets[0].asset_id == "asset_flux"
    assert report.song.prompt_id == "prompt-music"

    # And with every job settled, `active` goes false -- which is the browser's stop signal.
    for job in project.jobs:
        job.status = "complete"
    assert render_status_report(project).active is False
    assert render_status_report(Project(name="No song")).song is None


# --- Live progress on the poll answer ----------------------------------------------------


def test_the_report_carries_a_live_percentage_for_each_open_job_that_has_one():
    """The Director's ask, on the one answer the browser already polls: how far along is it.

    Attribution is by `prompt_id` and the row is keyed by `job_id`, so a batch of concurrent
    renders lands one row each with no chance of crossing. A job ComfyUI has said nothing about
    gets no row at all -- absence is how "unknown" is spelled, and it is what makes the surfaces
    fall back to exactly what they drew before this existed.
    """
    project = render_plan()
    report = render_status_report(project, progress={"prompt-flux": 35, "prompt-h3": 80})

    assert {row.job_id: row.percent for row in report.progress} == {
        "job_flux": 35,
        "job_h3": 80,
    }
    assert {row.prompt_id for row in report.progress} == {"prompt-flux", "prompt-h3"}
    # The music job is open too, and nothing has been said about it: no row, not a zero.
    assert "job_music" not in {row.job_id for row in report.progress}


def test_no_progress_at_all_is_an_empty_list_and_never_an_invented_zero():
    """The socket is down, or ComfyUI is. Every other field of the report is unchanged, and the
    percentage is simply absent -- a fabricated number is worse than none."""
    project = render_plan()

    assert render_status_report(project).progress == []
    assert render_status_report(project, progress={}).progress == []
    assert render_status_report(project, comfy_online=False).progress == []
    # ...and the rest of the answer is exactly what it was without the argument.
    assert render_status_report(project).model_dump(
        exclude={"progress"}
    ) == render_status_report(project, progress={"prompt-flux": 50}).model_dump(
        exclude={"progress"}
    )


def test_a_reported_zero_reaches_the_report_where_an_absent_one_does_not():
    project = render_plan()
    report = render_status_report(project, progress={"prompt-flux": 0})

    assert [(row.job_id, row.percent) for row in report.progress] == [("job_flux", 0)]


def test_a_settled_job_keeps_no_percentage_and_a_stranger_prompt_is_never_adopted():
    """Two ways a number could be wrong: left over from a render that finished, or belonging to
    somebody else's prompt entirely -- the socket is broadcast, so other clients' renders are on
    it too. Neither is reported."""
    project = render_plan()
    for job in project.jobs:
        job.status = "complete"

    assert render_status_report(project, progress={"prompt-flux": 60}).progress == []

    project.jobs[0].status = "queued"
    report = render_status_report(
        project, progress={"prompt-flux": 60, "somebody-elses-prompt": 90}
    )
    assert [(row.job_id, row.percent) for row in report.progress] == [("job_flux", 60)]


def test_a_percentage_out_of_range_is_clamped_rather_than_refused():
    """The model would reject anything outside 0-100 outright, and a poll answer that 500s
    because a number was odd is a worse failure than a clamped number."""
    project = render_plan()
    report = render_status_report(
        project, progress={"prompt-flux": 140, "prompt-h3": -20}
    )

    assert {row.job_id: row.percent for row in report.progress} == {
        "job_flux": 100,
        "job_h3": 0,
    }


def test_reporting_progress_writes_nothing_onto_the_project():
    """The load-bearing one. `render_status_report` is pure, and a percentage must not become a
    field on a job, a shot or the manifest -- see `comfy.ProgressTracker` for why."""
    project = render_plan()
    before = project.model_dump_json()

    render_status_report(project, progress={"prompt-flux": 35, "prompt-h3": 80})

    assert project.model_dump_json() == before
    # `RenderJob.progress` is the local ffmpeg export's persisted field (AD-9). No ComfyUI job
    # ever writes it, and a live percentage passing through this function must not start.
    assert [job.progress for job in project.jobs] == [0, 0, 0, 0]


# --- Supersession: the leftover record, and only it -------------------------------------


def superseding_plan() -> Project:
    """One shot with a stale open job, the new job that replaced it, and four bystanders.

    Every bystander differs from the stale record in exactly one way -- a different target,
    a different kind, an already-terminal status -- so a rule that widened along any one of
    those axes has something here to break.
    """
    return Project(
        name="Supersede",
        shots=[Shot(id="shot_a", start=0, duration=5, prompt="A corridor")],
        jobs=[
            RenderJob(id="job_stale", kind="h3", status="queued", prompt_id="p-1",
                      target_id="shot_a", missing_ticks=2),
            RenderJob(id="job_other_shot", kind="h3", status="running", prompt_id="p-2",
                      target_id="shot_b"),
            RenderJob(id="job_other_kind", kind="ltx", status="queued", prompt_id="p-3",
                      target_id="shot_a"),
            RenderJob(id="job_settled", kind="h3", status="complete", prompt_id="p-4",
                      target_id="shot_a", output_files=["takes/old.mp4"]),
            RenderJob(id="job_new", kind="h3", status="queued", prompt_id="p-5",
                      target_id="shot_a"),
        ],
    )


def test_supersede_settles_exactly_the_stale_record_and_leaves_the_new_one_open():
    project = superseding_plan()
    # Snapshot the bystanders before the call rather than rebuilding the plan afterwards:
    # `RenderJob.created_at`/`updated_at` default to the clock, so two constructions differ
    # whenever the clock ticks between them. That made this assertion fail about one run in
    # five for a reason that had nothing to do with supersession.
    before = {job.id: job.model_copy(deep=True) for job in project.jobs}

    changed = supersede_target_jobs(
        project, kinds={"h3"}, target_id="shot_a", keep_job_id="job_new"
    )

    assert [job.id for job in changed] == ["job_stale"]
    jobs = {job.id: job for job in project.jobs}
    # The stale record: settled, distinguishable, and still naming its prompt.
    assert jobs["job_stale"].status == "cancelled"
    assert jobs["job_stale"].error == JOB_SUPERSEDED
    assert jobs["job_stale"].superseded_by == "job_new"
    assert jobs["job_stale"].prompt_id == "p-1"
    # The counter meant "how close is this to being settled", and it is settled.
    assert jobs["job_stale"].missing_ticks == 0
    # The new job is untouched -- not settled, not marked, still the one to watch.
    assert jobs["job_new"].status == "queued"
    assert jobs["job_new"].superseded_by == ""
    assert jobs["job_new"].error == ""
    # Every bystander is byte-identical to how it arrived.
    for job_id in ("job_other_shot", "job_other_kind", "job_settled"):
        assert jobs[job_id] == before[job_id]


def test_supersession_releases_the_poll_and_is_a_no_op_the_second_time():
    """`active` is the browser's whole polling contract, and a settled record leaves it."""
    project = superseding_plan()
    assert {job.id for job in reconcilable_jobs(project)} == {
        "job_stale", "job_other_shot", "job_other_kind", "job_new"
    }

    supersede_target_jobs(project, kinds={"h3"}, target_id="shot_a", keep_job_id="job_new")

    assert "job_stale" not in {job.id for job in reconcilable_jobs(project)}
    # Idempotent for the same submission: `cancelled` is terminal, so repeating the call finds
    # nothing left to settle and cannot re-point a `superseded_by` that already names a job.
    assert supersede_target_jobs(
        project, kinds={"h3"}, target_id="shot_a", keep_job_id="job_new"
    ) == []
    assert next(job for job in project.jobs if job.id == "job_stale").superseded_by == "job_new"

    # A *third* render supersedes the second, and names itself -- the chain does not collapse
    # onto the first successor.
    project.jobs.append(
        RenderJob(id="job_third", kind="h3", status="queued", prompt_id="p-6", target_id="shot_a")
    )
    changed = supersede_target_jobs(
        project, kinds={"h3"}, target_id="shot_a", keep_job_id="job_third"
    )
    assert [job.id for job in changed] == ["job_new"]
    assert next(job for job in project.jobs if job.id == "job_new").superseded_by == "job_third"
    assert next(job for job in project.jobs if job.id == "job_stale").superseded_by == "job_new"


def test_a_superseded_record_is_distinguishable_from_a_hand_cancellation():
    """Both are `cancelled`; only one names a successor. That is the whole distinction."""
    project = superseding_plan()
    by_hand = next(job for job in project.jobs if job.id == "job_other_shot")
    by_hand.status = "cancelled"
    by_hand.error = "Cancelled by the Director before it finished."

    supersede_target_jobs(project, kinds={"h3"}, target_id="shot_a", keep_job_id="job_new")

    assert by_hand.superseded_by == ""
    assert next(job for job in project.jobs if job.id == "job_stale").superseded_by == "job_new"


def test_supersession_defaults_to_absent_on_every_manifest_written_before_it():
    """The field is defaulted, so a job record that predates it loads unchanged."""
    assert RenderJob(kind="h3").superseded_by == ""
    assert RenderJob.model_validate_json('{"kind": "h3"}').superseded_by == ""


# ----------------------------------------------------------------------------------------------
# Render timing.
#
# Until 2026-08-21 this application recorded nothing about what a render cost. `updated_at` was
# set by its default factory and no settle path ever wrote it again, so every settled job in the
# Director's live manifest carried `updated_at == created_at` to the microsecond -- and the one
# render-cost figure the codebase acted on turned out to be a comment citing itself, wrong by
# roughly 3.4x. These tests are the guarantee that the reconstruction never has to happen again.
# ----------------------------------------------------------------------------------------------


def test_format_duration_never_rounds_a_render_up_into_the_next_unit():
    """A render reported as `7m` when it took 6m59s is the smoothing this module exists to stop."""
    assert format_duration(0) == "0s"
    assert format_duration(42.9) == "42s"
    assert format_duration(59.999) == "59s"
    assert format_duration(60) == "1m00s"
    assert format_duration(378) == "6m18s"
    assert format_duration(419.9) == "6m59s"
    assert format_duration(3599) == "59m59s"
    assert format_duration(3600) == "1h00m"
    assert format_duration(7_500) == "2h05m"
    # Neither is a duration, and a clock adjustment produces the first.
    assert format_duration(-1) == "—"
    assert format_duration(float("inf")) == "—"
    assert format_duration(float("nan")) == "—"


def test_a_settle_writes_the_moment_it_ended_and_never_moves_the_enqueue_time():
    """The stamp itself, pinned on its own.

    `updated_at` used to be set by its `default_factory` and written again by nothing, so every
    settled job in the Director's live manifest read `updated_at == created_at` to the
    microsecond -- which is the fixture below, because that is what every manifest written
    before 2026-08-21 actually holds. A settle moves the one and leaves the other exactly where
    it was: `created_at` is the enqueue moment and is evidence, not bookkeeping.
    """
    job = RenderJob(kind="h3", target_id="shot_a")
    enqueued = job.created_at - timedelta(hours=2)
    job.created_at = enqueued
    job.updated_at = enqueued

    stamp_job_settled(job, elapsed_seconds=378.0)

    assert job.created_at == enqueued, "a settle must not move the enqueue time"
    assert job.updated_at > enqueued


def test_a_job_settled_before_the_instrumentation_is_never_given_a_duration_by_a_re_read():
    """The retroactive fabrication the transition check prevents.

    Every settled job in the Director's manifest predates this instrumentation and carries no
    measurement. A history re-read of one must leave it that way -- not record the hours between
    the render and whenever somebody next clicked refresh, which would be a fabricated
    measurement dressed as a recorded one, and the exact failure this whole change corrects.
    """
    project = render_plan()
    settled = next(item for item in project.jobs if item.id == "job_done")
    ended = settled.created_at - timedelta(days=1)
    settled.created_at = ended
    settled.updated_at = ended
    assert settled.status == "complete" and settled.render_seconds_source == ""

    apply_job_history(project, settled, completed("prompt-done", "singer_00001_.png"))

    assert settled.render_seconds == 0.0
    assert settled.render_seconds_source == ""
    assert settled.updated_at == ended


def test_a_settle_with_comfyuis_own_clock_records_the_render_and_says_where_it_came_from():
    job = RenderJob(kind="h3", target_id="shot_a", render_frames=141)
    job.created_at = job.created_at - timedelta(hours=3)

    assert stamp_job_settled(job, elapsed_seconds=378.0) is True

    assert job.render_seconds == 378.0
    assert job.render_seconds_source == JOB_TIMING_FROM_COMFY
    # The three-hour queue wait this record sat through is *not* in the number, which is the
    # whole reason ComfyUI's clock is preferred over the record's own span.
    assert job.updated_at > job.created_at


def test_a_settle_with_no_measurement_falls_back_to_the_record_and_labels_it_as_such():
    """`created_at` is enqueue, so the fallback is an upper bound and never a render time."""
    job = RenderJob(kind="h3", target_id="shot_a")
    job.created_at = job.created_at - timedelta(seconds=1800)

    assert stamp_job_settled(job) is True

    assert job.render_seconds == pytest.approx(1800, abs=5)
    assert job.render_seconds_source == JOB_TIMING_FROM_RECORD


def test_a_settle_is_idempotent_so_a_second_look_cannot_overwrite_the_real_measurement():
    """Terminal is terminal. A second call could only be a later clock replacing the truth --
    and it would make a re-read of a settled record look like a change to the reconciler, which
    would rewrite the manifest on a tick that learned nothing."""
    job = RenderJob(kind="h3", target_id="shot_a")
    stamp_job_settled(job, elapsed_seconds=378.0)
    stamped = job.updated_at

    assert stamp_job_settled(job, elapsed_seconds=9999.0) is False

    assert job.render_seconds == 378.0
    assert job.updated_at == stamped


def test_a_clock_that_ran_backwards_still_settles_the_job_but_claims_no_length():
    """No length, but the settle is still *recorded* as one.

    Leaving the source empty here was a half-stamped record: the field's empty value already
    means "settled before this application measured anything, and nothing was ever invented for
    it", which the queue panel says out loud -- about a job that settled just now. And because
    the idempotence guard keys on the source rather than on the stamp, the record stayed
    re-stampable forever, so any later settle path moved `updated_at` again.
    """
    job = RenderJob(kind="h3", target_id="shot_a", status="cancelled", prompt_id="pr1")
    job.created_at = job.created_at + timedelta(hours=1)

    assert stamp_job_settled(job) is True

    assert job.updated_at != job.created_at
    assert job.render_seconds == 0.0
    assert job.render_seconds_source == JOB_TIMING_UNMEASURED
    # Distinguishable from a job that predates the instrumentation, which is the whole point.
    assert render_timing_summary(RenderJob(kind="h3", target_id="shot_b")) == ""
    line = render_timing_summary(job)
    assert line == (
        "cancelled; the clock moved between this record being created and it settling, "
        "so no length was measured"
    )
    # And no number is invented on the way out: `0s` would be a claim about a render.
    assert "0s" not in line

    # Idempotent now, which it was not: the second call left the record alone entirely.
    stamped = job.updated_at - timedelta(days=3)
    job.updated_at = stamped
    assert stamp_job_settled(job) is False
    assert job.updated_at == stamped


def test_a_negative_or_non_finite_measurement_is_refused_in_favour_of_the_records_own_span():
    for offered in (-1.0, float("nan"), float("inf")):
        job = RenderJob(kind="h3", target_id="shot_a")
        job.created_at = job.created_at - timedelta(seconds=90)
        stamp_job_settled(job, elapsed_seconds=offered)
        assert job.render_seconds_source == JOB_TIMING_FROM_RECORD, offered
        assert job.render_seconds == pytest.approx(90, abs=5), offered


def test_the_surfaced_line_is_a_render_time_for_a_solo_job_and_carries_the_caveat_otherwise():
    """The caveat travels with the number, because a number read without it is how a 221-frame
    render came to be recorded as taking 2.2 hours."""
    solo = RenderJob(
        kind="h3", target_id="shot_a", status="complete", prompt_id="pr1", render_frames=141
    )
    stamp_job_settled(solo, elapsed_seconds=378.0)
    assert render_timing_summary(solo) == "rendered in 6m18s, 141 frames"

    queued = RenderJob(
        kind="h3", target_id="shot_b", status="complete", prompt_id="pr2",
        batch_id="batch_1", render_frames=226,
    )
    queued.created_at = queued.created_at - timedelta(seconds=1812)
    stamp_job_settled(queued)
    line = render_timing_summary(queued)
    assert line.startswith("30m12s from queued to done, 226 frames;")
    assert "the wait in the queue is included" in line
    assert "submitted in a batch" in line

    # Measured off the record but *not* part of a batch: the queue wait is still inside the
    # number and still said so, without claiming a batch that did not happen.
    alone = RenderJob(kind="h3", target_id="shot_c", status="complete", prompt_id="pr3")
    alone.created_at = alone.created_at - timedelta(seconds=95)
    stamp_job_settled(alone)
    assert "submitted in a batch" not in render_timing_summary(alone)
    assert "the wait in the queue is included" in render_timing_summary(alone)


def test_a_job_that_did_not_complete_is_never_described_as_having_rendered():
    """A cancellation that stood open for forty minutes rendered for some unknown part of that.
    "rendered in 40m" would be a fabrication."""
    job = RenderJob(
        kind="h3", target_id="shot_a", status="cancelled", prompt_id="pr1", render_frames=141
    )
    job.created_at = job.created_at - timedelta(seconds=2400)
    stamp_job_settled(job)

    line = render_timing_summary(job)

    assert line == (
        "cancelled after 40m00s, 141 frames (time the record was open, not render time)"
    )
    assert "rendered" not in line


def test_a_failure_comfyui_itself_timed_is_reported_as_render_time_and_not_as_a_record_span():
    """The inversion this branch order corrects.

    The status was consulted before the source, so a job that ComfyUI's *own execution clock*
    measured -- `execution_start` to `execution_error`, which is time on the GPU and nothing else
    -- was described as "the time the record was open, not render time". The exact opposite of
    what the number is, and on the one row a Director most needs to read: a 141-frame render that
    OOMs after three minutes is the cheapest cost datum this instrumentation can capture, and it
    was the one it mislabelled. The queue cell drew it with no `≤` all along, so the sentence
    also contradicted the column header it sat under.
    """
    died = RenderJob(
        kind="h3", target_id="shot_a", status="error", prompt_id="pr1", render_frames=141
    )
    # Enqueued three hours before it ran: none of that wait is in ComfyUI's measurement, which
    # is exactly why this job must not be described as a record span.
    died.created_at = died.created_at - timedelta(hours=3)
    stamp_job_settled(died, elapsed_seconds=192.0)

    line = render_timing_summary(died)

    assert died.render_seconds_source == JOB_TIMING_FROM_COMFY
    assert line == (
        "error after 3m12s of rendering, 141 frames (ComfyUI's own execution clock, so this is "
        "time on the GPU and not queue wait)"
    )
    assert "not render time" not in line
    # And the same job measured off the record keeps the old, correct sentence: there the span
    # really is only how long the record stood open.
    unmeasured = RenderJob(
        kind="h3", target_id="shot_a", status="error", prompt_id="pr1", render_frames=141
    )
    unmeasured.created_at = unmeasured.created_at - timedelta(hours=3)
    stamp_job_settled(unmeasured)
    assert "not render time" in render_timing_summary(unmeasured)


def test_a_finished_export_is_not_given_a_caveat_about_a_queue_it_was_never_in():
    """An assembly has an empty `prompt_id` by design -- it is local work and never goes near
    ComfyUI -- so "ComfyUI reported no execution clock for this prompt, so the wait in the queue
    is included" was a caveat invented out of nothing, about a component the job never touched,
    and it turned an exact export time into an apparent upper bound."""
    export = RenderJob(kind="post", target_id="assembly", prompt_id="", status="complete")
    export.created_at = export.created_at - timedelta(seconds=378)
    stamp_job_settled(export)

    line = render_timing_summary(export)

    assert export.render_seconds_source == JOB_TIMING_FROM_RECORD
    assert line == (
        "6m18s start to finish; local work that never went to ComfyUI, so this is the whole job "
        "rather than an upper bound"
    )
    assert "ComfyUI reported no execution clock" not in line

    # But only once it has finished. An export orphaned by a crash is settled at the next boot,
    # so its span runs to whenever somebody restarted the application -- a machine switched off
    # overnight, not a very slow export -- and that one is still reported as a record span.
    orphan = RenderJob(kind="post", target_id="assembly", prompt_id="", status="error")
    orphan.created_at = orphan.created_at - timedelta(hours=9)
    stamp_job_settled(orphan)
    assert "not render time" in render_timing_summary(orphan)


def test_a_job_with_no_recorded_timing_surfaces_nothing_rather_than_a_guess():
    """Every manifest written before 2026-08-21 is this case, and none of them may grow a number."""
    assert render_timing_summary(RenderJob(kind="h3", target_id="shot_a")) == ""
    assert render_timing_summary(RenderJob(kind="h3", status="complete")) == ""


def test_the_timing_fields_default_so_every_manifest_written_before_them_loads_unchanged():
    stored = RenderJob.model_validate_json('{"kind": "h3", "id": "job_old"}')

    assert stored.render_seconds == 0.0
    assert stored.render_seconds_source == ""
    assert stored.render_frames == 0
    # And "never settled" stays legible as itself rather than becoming a zero-length render.
    assert render_timing_summary(stored) == ""


def test_a_completion_records_the_render_from_comfyuis_clock_when_it_has_one():
    project = render_plan()
    job = next(item for item in project.jobs if item.id == "job_flux")
    job.created_at = job.created_at - timedelta(seconds=4000)
    history = HistoryResult(
        prompt_id="prompt-flux",
        status="complete",
        outputs=[{"subfolder": "out", "filename": "singer_00001_.png"}],
        started_ms=1_724_000_000_000,
        finished_ms=1_724_000_212_000,
    )

    apply_job_history(project, job, history)

    assert job.status == "complete"
    assert job.render_seconds == pytest.approx(212.0)
    assert job.render_seconds_source == JOB_TIMING_FROM_COMFY


def test_a_completion_with_no_clock_falls_back_to_the_record_and_a_re_read_changes_nothing():
    """The per-job refresh route runs `apply_job_history` on the same answer twice. A settled
    job re-read must not be re-dated -- see `stamp_job_settled`'s idempotence."""
    project = render_plan()
    job = next(item for item in project.jobs if item.id == "job_flux")
    history = completed("prompt-flux", "singer_00001_.png")

    apply_job_history(project, job, history)
    first = (job.render_seconds, job.render_seconds_source, job.updated_at)
    apply_job_history(project, job, history)

    assert job.render_seconds_source == JOB_TIMING_FROM_RECORD
    assert (job.render_seconds, job.render_seconds_source, job.updated_at) == first


def test_a_queued_to_running_move_is_not_a_settle_and_stamps_nothing():
    """`updated_at` means "when this ended". If a status move that is not an ending wrote it,
    `updated_at - created_at` would stop being a duration."""
    project = render_plan()
    job = next(item for item in project.jobs if item.id == "job_flux")
    created = job.created_at

    apply_job_history(project, job, HistoryResult(prompt_id="prompt-flux", status="running"))

    assert job.status == "running"
    assert job.updated_at == created
    assert job.render_seconds_source == ""


async def test_the_missing_ticks_death_stamps_the_record_it_settles():
    """No ComfyUI measurement exists by definition here -- this branch is reached *because*
    ComfyUI has no record of the prompt -- so the span is the record's own and is labelled so."""
    project = render_plan()
    job = next(item for item in project.jobs if item.id == "job_flux")
    job.created_at = job.created_at - timedelta(seconds=600)
    comfy = ScriptedComfy(
        histories={
            "prompt-flux": HistoryResult(prompt_id="prompt-flux", status="queued", known=False)
        }
    )

    for _ in range(MISSING_TICKS_LIMIT):
        await reconcile_render_jobs(project, comfy)

    assert job.status == "error"
    assert job.error == JOB_LOST_WITH_QUEUE
    assert job.render_seconds_source == JOB_TIMING_FROM_RECORD
    assert job.render_seconds == pytest.approx(600, abs=5)
    assert job.updated_at > job.created_at


def test_supersession_stamps_the_record_and_does_not_call_it_a_render():
    """An older prompt may still be executing on ComfyUI as this runs, so what is recorded is
    how long the *record* stood open -- and the surfaced line says exactly that."""
    project = superseding_plan()
    stale = next(job for job in project.jobs if job.id == "job_stale")
    stale.created_at = stale.created_at - timedelta(seconds=125)

    supersede_target_jobs(project, kinds={"h3"}, target_id="shot_a", keep_job_id="job_new")

    assert stale.status == "cancelled"
    assert stale.render_seconds == pytest.approx(125, abs=5)
    assert stale.render_seconds_source == JOB_TIMING_FROM_RECORD
    assert "not render time" in render_timing_summary(stale)
    # The bystanders are untouched, timing included: a settle stamps the record it settles.
    bystander = next(job for job in project.jobs if job.id == "job_other_shot")
    assert bystander.render_seconds_source == ""
