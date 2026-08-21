"""The readiness gate's own truth table, over the pure report and nothing else.

`tests/test_timeline.py` is the precedent: a pure module is tested directly, with an in-memory
`Project` and no client, so the rules are asserted rather than inferred from a route's status code.
"""

from pathlib import Path
from typing import get_args

from music_video_producer.batch import (
    JOB_LOST_WITH_QUEUE,
    JOB_SUPERSEDED,
    MISSING_TICKS_LIMIT,
    NEAR_DUPLICATE_OVERLAP,
    PLACEHOLDER_PROMPT,
    PLAN_WITHOUT_SHOTS,
    READINESS_REFUSAL,
    REFUSAL_NAME_LIMIT,
    SHOT_WITH_PLACEHOLDER_PROMPT,
    SHOT_WITHOUT_PROMPT,
    SHOTS_LACK_VARIANCE,
    SHOTS_SHARE_ONE_PROMPT,
    TERMINAL_JOB_STATUSES,
    VARIANCE_WARNING_LIMIT,
    AssetRenderState,
    ShotRenderState,
    SongRenderState,
    _overlap,
    _words,
    prompt_is_missing,
    prompt_rejection,
    queue_locations,
    readiness_refusal,
    readiness_report,
    reconcilable_jobs,
    reconcile_render_jobs,
    render_status_report,
    shot_label,
    supersede_target_jobs,
)
from music_video_producer.comfy import ComfyError, HistoryResult
from music_video_producer.models import (
    Asset,
    JobStatus,
    Project,
    RenderJob,
    Shot,
    Song,
    VisionInspectionRecord,
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
