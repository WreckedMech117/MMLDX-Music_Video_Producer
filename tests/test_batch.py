"""The readiness gate's own truth table, over the pure report and nothing else.

`tests/test_timeline.py` is the precedent: a pure module is tested directly, with an in-memory
`Project` and no client, so the rules are asserted rather than inferred from a route's status code.
"""

from pathlib import Path

from music_video_producer.batch import (
    NEAR_DUPLICATE_OVERLAP,
    PLACEHOLDER_PROMPT,
    PLAN_WITHOUT_SHOTS,
    READINESS_REFUSAL,
    REFUSAL_NAME_LIMIT,
    SHOT_WITH_PLACEHOLDER_PROMPT,
    SHOT_WITHOUT_PROMPT,
    SHOTS_LACK_VARIANCE,
    SHOTS_SHARE_ONE_PROMPT,
    VARIANCE_WARNING_LIMIT,
    _overlap,
    _words,
    prompt_is_missing,
    prompt_rejection,
    readiness_refusal,
    readiness_report,
    shot_label,
)
from music_video_producer.models import Project, Shot


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
