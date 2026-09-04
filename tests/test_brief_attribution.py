"""Editing a range clears its mark: the Brief's attribution and the one reconciliation (14.3).

Slice C's first half — the server side of TP-8, binding AD-33 and AD-45. Its own module for
`test_planning.py`'s reason: the guarantees belong to one feature and span three layers — a pure
function, the single document writer it hangs off, and four routes that write the Brief between
them.

**The reconciliation is asserted by comparison and nothing else.** `RECONCILIATIONS` below is a
table of `(stored text, stored ranges, new text) → expected ranges`, compared whole. A test that
checked a shape — "the survivors are still sorted", "no range exceeds the text" — would pass for
a function that dropped every mark, which is precisely the failure mode a provenance feature has:
losing marks is silent, and inventing them is worse than silent.

**The wiring is asserted separately from the decision, and that is the point of the second half
of this module.** The recorded survivor of this repository's last five mutation runs has been a
pure decision covered by a test and the caller that calls it covered by nothing, so every writer
of the Brief there is — the Director's save, the planning turn, Suggest Video and the restore —
is driven through its own route here and asserted on the stored manifest.
"""

import ast
import itertools
import json

import pytest
from package_source import module_name, package_modules
from test_api import (
    RefusingDirector,
    RevisingDirector,
    documented_project,
    make_client,
    make_client_with_director,
    project_documents,
)
from test_planning import PlanningDirector, planning_project, turn
from test_suggest_video import SUGGEST, SuggestingDirector, suggestable_project, suggestion

from music_video_producer.app import (
    ATTRIBUTED_DOCUMENTS,
    DIRECTOR_CONTEXT_EXCLUDE,
    DOCUMENT_LABELS,
    DOCUMENT_WRITER_MACHINE,
    DOCUMENT_WRITER_SAVE,
    RECOVERY_SLOT_SUFFIX,
    ProjectDocumentsRequest,
    new_stretches,
    reconcile_attribution,
    write_document,
    written_attribution,
)
from music_video_producer.models import BriefRange, Project
from music_video_producer.store import ProjectStore

TURN = "/api/projects/{project}/planning/turn"

#: The `Project` field the one attributed document keeps its marks in, read off the mapping
#: rather than typed, so this module cannot be testing a field the application stopped writing.
ATTRIBUTION_FIELD = ATTRIBUTED_DOCUMENTS["creative_brief"]


def mark(start: int, end: int, message_id: str = "") -> BriefRange:
    return BriefRange(start=start, end=end, message_id=message_id)


def marks(project: Project) -> list[tuple[int, int, str]]:
    """A project's attribution as plain tuples, and the marked text is checked against it."""
    return [(one.start, one.end, one.message_id) for one in project.brief_attribution]


def marked_text(project: Project) -> list[str]:
    """What each mark actually covers in the stored Brief — the claim a reader would see."""
    return [project.creative_brief[one.start : one.end] for one in project.brief_attribution]


# ---------------------------------------------------------------------------------------------
# The reconciliation, by comparison
# ---------------------------------------------------------------------------------------------

#: `(name, stored text, stored ranges, new text, expected ranges)`.
#:
#: Every row of this table is a whole answer compared whole. The five the spec names are here by
#: name — an insertion before a mark, an edited mark, a mark deleted outright, two adjacent
#: marks, and a mark at offset 0 and at the very end — with the states that have no acceptance
#: line of their own but decide whether the others mean anything: the empty list, the unchanged
#: text, a repeated run, and a mark that does not describe the stored text at all.
RECONCILIATIONS: list[tuple[str, str, list[BriefRange], str, list[BriefRange]]] = [
    (
        "an insertion before a mark shifts it by exactly the insertion's length",
        "Alpha beta. Gamma delta.",
        [mark(12, 24, "msg_one")],
        "Alpha beta. Inserted. Gamma delta.",
        [mark(22, 34, "msg_one")],
    ),
    (
        "an insertion after a mark leaves it exactly where it was",
        "Alpha beta. Gamma delta.",
        [mark(0, 11, "msg_one")],
        "Alpha beta. Gamma delta. And more.",
        [mark(0, 11, "msg_one")],
    ),
    (
        "a mark whose text was edited is dropped, which is the whole feature",
        "Alpha beta. Gamma delta.",
        [mark(12, 24, "msg_one")],
        "Alpha beta. Gamma epsilon.",
        [],
    ),
    (
        "one word changed inside a long mark drops the whole mark",
        "The wolf is seen four times and never twice the same.",
        [mark(0, 53, "msg_one")],
        "The wolf is seen three times and never twice the same.",
        [],
    ),
    (
        "a mark deleted outright is dropped",
        "One. Two. Three.",
        [mark(5, 9, "msg_one")],
        "One. Three.",
        [],
    ),
    (
        "two adjacent marks stay adjacent, stay in order and keep their own ids",
        "ABCD",
        [mark(0, 2, "msg_one"), mark(2, 4, "msg_two")],
        "xxABCD",
        [mark(2, 4, "msg_one"), mark(4, 6, "msg_two")],
    ),
    (
        "a mark at offset 0 survives at offset 0 when nothing before it moved",
        "First line. Second line.",
        [mark(0, 10, "msg_one")],
        "First line. Second line. Third line.",
        [mark(0, 10, "msg_one")],
    ),
    (
        "a mark ending at the very last character survives",
        "one two",
        [mark(4, 7, "msg_one")],
        "one two three",
        [mark(4, 7, "msg_one")],
    ),
    (
        "the whole document marked, then typed in front of",
        "A night drive.",
        [mark(0, 14, "msg_one")],
        "Note. A night drive.",
        [mark(6, 20, "msg_one")],
    ),
    (
        "one of two marks edited: the other still moves correctly",
        "Keep this. Edit this.",
        [mark(0, 10, "msg_one"), mark(11, 21, "msg_two")],
        "New. Keep this. Edited this.",
        [mark(5, 15, "msg_one")],
    ),
    (
        "a run moved behind a later run keeps the order and drops the one it crossed",
        "X. Y.",
        [mark(0, 2, "msg_one"), mark(3, 5, "msg_two")],
        "Y. Q. X.",
        [mark(6, 8, "msg_one")],
    ),
    (
        "two identical runs, one of them deleted: the survivor is marked once, not twice",
        "abab",
        [mark(0, 2, "msg_one"), mark(2, 4, "msg_two")],
        "ab",
        [mark(0, 2, "msg_one")],
    ),
    (
        "marks arriving out of order are placed in text order, not in list order",
        "Alpha beta. Gamma delta.",
        [mark(12, 24, "msg_two"), mark(0, 11, "msg_one")],
        "New. Alpha beta. Gamma delta.",
        [mark(5, 16, "msg_one"), mark(17, 29, "msg_two")],
    ),
    (
        "an empty list is the identity, which is every Brief written before this field",
        "A Brief nobody marked.",
        [],
        "A Brief nobody marked, then edited.",
        [],
    ),
    (
        "unchanged text is the identity",
        "Alpha beta. Gamma delta.",
        [mark(0, 11, "msg_one"), mark(12, 24, "msg_two")],
        "Alpha beta. Gamma delta.",
        [mark(0, 11, "msg_one"), mark(12, 24, "msg_two")],
    ),
    (
        "a repeated run keeps the occurrence nearest where the mark was",
        "red. blue. red.",
        [mark(11, 15, "msg_one")],
        "red. blue. green. red.",
        [mark(18, 22, "msg_one")],
    ),
    (
        "two marks on identical text stay one each rather than collapsing onto the first",
        "red. red.",
        [mark(0, 4, "msg_one"), mark(5, 9, "msg_two")],
        "x red. red.",
        [mark(2, 6, "msg_one"), mark(7, 11, "msg_two")],
    ),
    (
        "everything deleted drops everything",
        "Alpha beta. Gamma delta.",
        [mark(0, 11, "msg_one"), mark(12, 24, "msg_two")],
        "",
        [],
    ),
    (
        "a mark that runs past the stored text describes nothing and is dropped",
        "Short.",
        [mark(0, 40, "msg_one")],
        "Short.",
        [],
    ),
    (
        "an empty mark describes nothing and is dropped",
        "Alpha beta.",
        [mark(3, 3, "msg_one")],
        "Alpha beta.",
        [],
    ),
    (
        "an inverted mark describes nothing and is dropped",
        "Alpha beta.",
        [mark(7, 3, "msg_one")],
        "Alpha beta.",
        [],
    ),
]


@pytest.mark.parametrize(
    ("stored", "ranges", "text", "expected"),
    [row[1:] for row in RECONCILIATIONS],
    ids=[row[0] for row in RECONCILIATIONS],
)
def test_the_reconciliation_is_asserted_by_comparison(stored, ranges, text, expected):
    """AD-33's function, compared whole against a table rather than property-checked.

    The comparison is of the *list*, so a row proves the offsets, the ids, the order and the
    count at once — and a function that quietly dropped every mark, which is the failure a
    shape-check cannot see, fails on the first row.
    """
    assert reconcile_attribution(stored, ranges, text) == expected


def test_the_reconciliation_marks_the_text_it_claims_to_mark():
    """The property the table cannot state: a survivor covers the same characters it used to.

    Offsets are only meaningful against the text they index, and the table asserts numbers. This
    reads the marked run back out of *both* strings and compares them, which is what a Director
    would see and what C2's overlay will draw.
    """
    stored = "The corridor of headlights. The treeline. The forest past it."
    ranges = [mark(0, 26, "msg_one"), mark(27, 40, "msg_two")]
    text = f"An opening line. {stored}"

    for before, after in zip(ranges, reconcile_attribution(stored, ranges, text), strict=True):
        assert stored[before.start : before.end] == text[after.start : after.end]
        assert before.message_id == after.message_id


def test_the_reconciliation_never_invents_a_mark_or_returns_the_objects_it_was_given():
    """Purity, in the two ways it is load-bearing here.

    Nothing is added: the output is at most as long as the input, because there is no arm of this
    function that creates a mark. And the stored ranges are not mutated in place — this runs
    inside `write_document`, which is called mid-request on the live project object, so a
    function that edited its argument would rewrite history rather than describe it.
    """
    ranges = [mark(0, 11, "msg_one"), mark(12, 24, "msg_two")]
    before = [(one.start, one.end, one.message_id) for one in ranges]

    survivors = reconcile_attribution("Alpha beta. Gamma delta.", ranges, "Gamma delta.")

    assert len(survivors) <= len(ranges)
    assert [(one.start, one.end, one.message_id) for one in ranges] == before
    assert all(one not in ranges for one in survivors)


# ---------------------------------------------------------------------------------------------
# The two arms: who marks, and who only reconciles
# ---------------------------------------------------------------------------------------------


def test_a_machine_write_over_a_wholly_replaced_document_marks_all_of_it():
    """AD-45's positive half, in the case where *everything* the write produced is new.

    This used to be the rule — a machine write marked the whole document, full stop — and the
    Director's ruling of 2026-09-04 demoted it to what it is here: a **consequence**. Nothing of
    the old text survives into the new, so every line is new, and the complement of nothing is
    the whole document. The rule now is *mark what changed*, and the two tests below it are what
    that rule actually says.

    The id travels because *"which turn wrote this"* is the question the mark exists to answer
    (AD-43); a mark with nothing to point at is a coloured background.
    """
    assert written_attribution(
        "The Director's own Brief.",
        [mark(0, 25, "msg_old")],
        "A Brief the model wrote.",
        writer=DOCUMENT_WRITER_MACHINE,
        message_id="msg_new",
    ) == [mark(0, 24, "msg_new")]


# ---------------------------------------------------------------------------------------------
# What a machine write actually wrote (Director's ruling, 2026-09-04)
# ---------------------------------------------------------------------------------------------

#: The Director's own Brief, in the shape `compose_brief` writes and a Director then edits:
#: markdown headings, one section per blank-line-separated block.
DIRECTOR_BRIEF = (
    "## Premise\nA night drive that opens into wilderness.\n\n## Cast\nOne driver, alone."
)
#: What a planning turn returns when asked to add a wolf and a Look: the Director's premise
#: **verbatim**, a rewritten cast, and a section that was not there. This is the worked example
#: the ruling was decided on.
TURNED_BRIEF = (
    "## Premise\nA night drive that opens into wilderness.\n\n"
    "## Cast\nOne driver and one wolf.\n\n"
    "## Look\nSodium amber against deep blacks."
)
#: Everything from the rewritten cast to the end — one contiguous stretch, because the blank line
#: between the new cast and the new Look is not a kept line and so does not split them.
TURNED_NEW = TURNED_BRIEF[TURNED_BRIEF.index("One driver and one wolf.") :]

#: `(name, stored text, new text, expected stretches)`, with every stretch attributed to `"msg"`.
STRETCHES: list[tuple[str, str, str, list[BriefRange]]] = [
    (
        "a wholly new document is one stretch from 0 to the end",
        "",
        "## Premise\nA night drive.",
        [mark(0, 25, "msg")],
    ),
    (
        "the ruling's example: premise kept, cast and look marked as one stretch",
        DIRECTOR_BRIEF,
        TURNED_BRIEF,
        [mark(len(TURNED_BRIEF) - len(TURNED_NEW), len(TURNED_BRIEF), "msg")],
    ),
    (
        "a line kept verbatim is not new even when it moved",
        "Second.\nFirst.",
        "First.\nSecond.",
        [],
    ),
    (
        "text identical to the stored text has nothing new in it",
        "Alpha.",
        "Alpha.",
        [],
    ),
    (
        "a blank line between two new stretches does not split them",
        "Old.",
        "New one.\n\nNew two.",
        [mark(0, 18, "msg")],
    ),
    (
        "a kept line between two new stretches does split them",
        "Kept.",
        "New one.\nKept.\nNew two.",
        [mark(0, 8, "msg"), mark(15, 23, "msg")],
    ),
    (
        "a blanked document has nothing new in it",
        "Something.",
        "",
        [],
    ),
    (
        "a paragraph the model re-wrapped is not new: presence is asked as a substring",
        "A night drive that opens into wilderness, told in three movements.",
        "A night drive that opens into wilderness,\ntold in three movements.",
        [],
    ),
    (
        "a stray space on a blank line does not split the stretch around it",
        "The old brief.",
        "New one.\n \nNew two.",
        [mark(0, 19, "msg")],
    ),
    (
        "whitespace is never a unit and never the edge of a stretch",
        "",
        "  \nReal line.\n  ",
        [mark(3, 13, "msg")],
    ),
]


@pytest.mark.parametrize(
    ("stored", "text", "expected"),
    [row[1:] for row in STRETCHES],
    ids=[row[0] for row in STRETCHES],
)
def test_what_a_machine_write_added_is_asserted_by_comparison(stored, text, expected):
    """The ruling's half of the decision, compared whole against a table like the other one.

    *A stretch is new when it is not present in the stored text* — asked one line at a time,
    which is finer than the paragraph the ruling priced and needs no diff. The row that matters
    most is the second: it is the Director's own worked example, and it is the one a function
    that went back to marking the whole document would fail.
    """
    assert new_stretches(stored, text, message_id="msg") == expected


def test_a_machine_write_keeps_the_directors_paragraph_and_marks_only_what_it_added():
    """The ruling, in the pure function: two sets, both present, neither swallowing the other.

    The Director's premise survives unmarked *and* an earlier turn's mark on it survives marked,
    which are different claims — the first is about what is absent from the answer and the second
    about what is present in it.
    """
    stored = "## Cast\nOne driver.\n\n## Look\nAmber."
    written = "## Cast\nOne driver.\n\n## Look\nSodium amber against deep blacks."

    assert written_attribution(
        stored,
        [mark(8, 19, "msg_earlier")],
        written,
        writer=DOCUMENT_WRITER_MACHINE,
        message_id="msg_new",
    ) == [
        # The earlier turn's mark on "One driver.", returned verbatim and still its own.
        mark(8, 19, "msg_earlier"),
        # The rewritten Look, and only from its first written character.
        mark(written.index("Sodium"), len(written), "msg_new"),
    ]


def test_a_surviving_mark_inside_a_rewritten_line_is_dropped_and_the_writing_turn_wins():
    """Point 3, and **the assumption in it does not hold**: the two sets are not disjoint.

    A surviving mark sits in text present in both versions, so it reads as though it could never
    fall inside a stretch declared new — and it can, because the two questions are asked at
    different sizes. `One driver` survives verbatim into `One driver walks alone.`, whose *line*
    is new. Both claims are true, and provenance cannot hold two.

    The decision is that the writing turn wins, made explicitly rather than fallen into: the turn
    that emitted the line now on the page is the honest answer, and the alternative — clipping
    the older mark into fragments around it — invents boundaries neither turn wrote.
    """
    stored = "## Cast\nOne driver."
    written = "## Cast\nOne driver walks alone."

    survivor = reconcile_attribution(stored, [mark(8, 18, "msg_old")], written)
    fresh = new_stretches(stored, written, message_id="msg_new")
    # The premise of the whole test: on their own, these two overlap.
    assert survivor == [mark(8, 18, "msg_old")]
    assert fresh == [mark(8, 31, "msg_new")]

    assert written_attribution(
        stored, [mark(8, 18, "msg_old")], written,
        writer=DOCUMENT_WRITER_MACHINE, message_id="msg_new",
    ) == [mark(8, 31, "msg_new")]


@pytest.mark.parametrize(
    ("stored", "text", "expected"),
    [row[1:] for row in STRETCHES],
    ids=[row[0] for row in STRETCHES],
)
def test_no_machine_write_ever_returns_two_marks_over_one_character(stored, text, expected):
    """Point 3 asserted rather than assumed, over every row of both tables at once.

    Disjointness is what makes *"which turn wrote this character"* a question with one answer,
    and it is a property of the combined result rather than of either half. A stored range list
    that deliberately marks everything is used, so the survivors have every chance to collide.
    """
    everything = [mark(0, len(stored), "msg_old")] if stored else []

    written = written_attribution(
        stored, everything, text, writer=DOCUMENT_WRITER_MACHINE, message_id="msg_new"
    )

    assert written == sorted(written, key=lambda one: (one.start, one.end))
    for before, after in itertools.pairwise(written):
        assert before.end <= after.start, (before, after)
    assert all(0 <= one.start < one.end <= len(text) for one in written)


def test_the_ruling_holds_through_the_planning_route(tmp_path):
    """Point 5: the wiring, which is what keeps surviving. The Director's example, end to end.

    A Brief the Director wrote, a turn that returns one of their paragraphs verbatim and adds to
    it, and the assertion the ruling is actually about — **their premise is left unmarked**. The
    pure function above is not evidence for this: the route has to hand the write the stored text
    it is replacing, and a caller that passed the new text twice would satisfy every test in the
    first half of this module while marking the whole document here.
    """
    client, store, _ = make_client(tmp_path, director=PlanningDirector(turn(brief=TURNED_BRIEF)))
    project = planning_project(store, brief=DIRECTOR_BRIEF)

    response = client.post(
        TURN.format(project=project.id), json={"message": "add a wolf", "apply_documents": True}
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.creative_brief == TURNED_BRIEF
    reply = next(one for one in reversed(saved.messages) if one.role == "assistant")
    assert marked_text(saved) == [TURNED_NEW]
    assert marks(saved) == [(len(TURNED_BRIEF) - len(TURNED_NEW), len(TURNED_BRIEF), reply.id)]
    # The sentence the Director typed is not inside any mark — the claim, stated as the claim.
    assert "A night drive that opens into wilderness." not in TURNED_NEW


def test_an_earlier_turns_mark_survives_a_later_turn_through_the_route(tmp_path):
    """Point 1 through the route: a mark from one turn and a mark from the next, side by side.

    This is what makes the thread worth pointing at — two ids in one Brief, each naming the turn
    that actually wrote its stretch. A machine arm that cleared the marks before adding its own
    would pass every whole-document test in this module and fail here.
    """
    client, store, _ = make_client(tmp_path, director=PlanningDirector(turn(brief=TURNED_BRIEF)))
    project = planning_project(store, brief=DIRECTOR_BRIEF)
    premise = DIRECTOR_BRIEF.index("A night drive")
    project.brief_attribution = [
        mark(premise, premise + len("A night drive that opens into wilderness."), "msg_earlier")
    ]
    store.save(project)

    client.post(
        TURN.format(project=project.id), json={"message": "add a wolf", "apply_documents": True}
    )

    saved = ProjectStore(tmp_path).get(project.id)
    reply = next(one for one in reversed(saved.messages) if one.role == "assistant")
    assert marked_text(saved) == ["A night drive that opens into wilderness.", TURNED_NEW]
    assert [one.message_id for one in saved.brief_attribution] == ["msg_earlier", reply.id]


def test_a_machine_write_of_nothing_marks_nothing():
    """A blanked document has no run to mark, and `(0, 0)` is a mark over no characters."""
    assert (
        written_attribution(
            "Something.", [mark(0, 10)], "", writer=DOCUMENT_WRITER_MACHINE, message_id="msg_new"
        )
        == []
    )


def test_a_save_only_ever_reconciles_and_never_marks():
    """AD-45's negative half, and constraint 5: nothing about the Director's own text is recorded.

    The unmarked default is theirs. A save over a Brief with no marks produces no marks — if it
    produced one, the very first thing a Director typed would be attributed to the assistant.
    """
    assert (
        written_attribution(
            "", [], "Every word of this is mine.", writer=DOCUMENT_WRITER_SAVE, message_id="msg"
        )
        == []
    )
    assert written_attribution(
        "Alpha beta. Gamma delta.",
        [mark(12, 24, "msg_one")],
        "Alpha beta. Inserted. Gamma delta.",
        writer=DOCUMENT_WRITER_SAVE,
        message_id="ignored",
    ) == [mark(22, 34, "msg_one")]


def test_a_byte_equal_machine_write_takes_authorship_of_nothing():
    """A pass that returned what was already on the page did not write the Director's Brief.

    This is `write_document`'s own byte-equal rule, and it bites harder here than it does on the
    recovery slot: the slot loses a recoverable version, while this would put the assistant's
    name on an hour of the Director's revisions on the strength of a no-op.
    """
    assert (
        written_attribution(
            "The Director typed every word of this.",
            [],
            "The Director typed every word of this.",
            writer=DOCUMENT_WRITER_MACHINE,
            message_id="msg_new",
        )
        == []
    )


def test_write_document_leaves_the_documents_that_have_no_attribution_alone():
    """Constraint: only the Brief is attributed, and `ATTRIBUTED_DOCUMENTS` is where that is said.

    A machine write of the Treatment must not touch the Brief's marks — the Treatment is made
    *from* the Brief, and a mark that moved because a different document was written would be
    provenance describing the wrong text.
    """
    project = Project(name="Two documents")
    project.creative_brief = "Alpha beta. Gamma delta."
    project.brief_attribution = [mark(12, 24, "msg_one")]

    write_document(project, "treatment", "A treatment.", writer=DOCUMENT_WRITER_MACHINE)
    write_document(project, "style_bible", "A style bible.", writer=DOCUMENT_WRITER_SAVE)

    assert marks(project) == [(12, 24, "msg_one")]
    assert set(ATTRIBUTED_DOCUMENTS) == {"creative_brief"}


# ---------------------------------------------------------------------------------------------
# Every writer of the Brief, through its own route
# ---------------------------------------------------------------------------------------------


def marked_project(store: ProjectStore, name: str = "Marked") -> Project:
    """A project whose Brief carries one assistant-written run in the middle of it."""
    project = documented_project(store, name)
    project.creative_brief = "Alpha beta. Gamma delta."
    project.brief_attribution = [mark(12, 24, "msg_one")]
    store.save(project)
    return store.get(project.id)


def test_a_save_that_shifts_a_marked_run_moves_its_mark(tmp_path):
    """Acceptance 1, through the route the Director's Save button actually calls.

    Re-read through a fresh `ProjectStore`, because the response body is the object the handler
    just built and only the manifest proves the mark was stored.
    """
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = marked_project(store)

    response = client.put(
        f"/api/projects/{project.id}/documents",
        json=project_documents(project, creative_brief="Alpha beta. Inserted. Gamma delta."),
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert marks(saved) == [(22, 34, "msg_one")]
    assert marked_text(saved) == ["Gamma delta."]


def test_a_save_that_edits_a_marked_run_clears_its_mark(tmp_path):
    """Acceptance 2, and the sentence this slice is named after.

    The Director edits the assistant's paragraph; the mark goes. What is left is unmarked, which
    reads as theirs — which it now is.
    """
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = marked_project(store)

    client.put(
        f"/api/projects/{project.id}/documents",
        json=project_documents(project, creative_brief="Alpha beta. Gamma epsilon."),
    )

    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.creative_brief == "Alpha beta. Gamma epsilon."
    assert marks(saved) == []


def test_an_ordinary_save_carries_text_only_and_a_body_that_sends_ranges_is_ignored(tmp_path):
    """Acceptance 3 and AD-45: the server is the sole writer, so the wire has no field for this.

    Two halves. The request model has no member — which is what makes the browser's payload
    correct by construction, and `test_frontend_contract`'s document-save contract already pins
    that payload to exactly this model's fields. And a hand-rolled body that adds the key anyway
    changes nothing, because an unknown key on this model is dropped rather than bound.
    """
    assert ATTRIBUTION_FIELD not in ProjectDocumentsRequest.model_fields
    assert set(ProjectDocumentsRequest.model_fields) == {
        "creative_brief",
        "treatment",
        "style_bible",
        "creative_brief_locked",
        "treatment_locked",
        "style_bible_locked",
    }

    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = marked_project(store)
    body = project_documents(project, creative_brief="Alpha beta. Inserted. Gamma delta.")
    body[ATTRIBUTION_FIELD] = [{"start": 0, "end": 11, "message_id": "forged"}]

    assert client.put(f"/api/projects/{project.id}/documents", json=body).status_code == 200

    saved = ProjectStore(tmp_path).get(project.id)
    assert marks(saved) == [(22, 34, "msg_one")]


def test_a_planning_turn_marks_the_brief_it_wrote_and_names_itself(tmp_path):
    """Acceptance 7: the range a planning write creates names the message that wrote it (AD-43).

    Asserted against the stored thread rather than against a string this test made up — the
    claim is that a Director reading the manifest six months later can follow the id to the turn,
    so the id has to be one that is actually in `project.messages`.
    """
    revised = "A night drive that opens into wilderness, told in three movements."
    client, store, _ = make_client(tmp_path, director=PlanningDirector(turn(brief=revised)))
    project = planning_project(store)

    response = client.post(
        TURN.format(project=project.id), json={"message": "revise it", "apply_documents": True}
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.creative_brief == revised
    reply = next(one for one in reversed(saved.messages) if one.role == "assistant")
    assert marks(saved) == [(0, len(revised), reply.id)]
    assert marked_text(saved) == [revised]


def test_a_second_planning_turn_replaces_the_first_turns_mark_with_its_own(tmp_path):
    """Provenance is about the text that is there now, not about every turn that ever ran.

    The second turn rewrites the whole Brief, so the first turn's mark describes text that no
    longer exists and must not survive pointing at the new text.
    """
    first, second = "The first model Brief.", "The second model Brief, entirely rewritten."
    client, store, _ = make_client(
        tmp_path, director=PlanningDirector(turn(brief=first), turn(brief=second))
    )
    project = planning_project(store)

    for message in ("first", "second"):
        client.post(
            TURN.format(project=project.id), json={"message": message, "apply_documents": True}
        )

    saved = ProjectStore(tmp_path).get(project.id)
    replies = [one for one in saved.messages if one.role == "assistant"]
    assert saved.creative_brief == second
    assert marks(saved) == [(0, len(second), replies[-1].id)]
    assert replies[0].id != replies[-1].id


def test_a_planning_turn_that_was_refused_marks_nothing(tmp_path):
    """Capture on apply, never on attempt — the mark follows the write, not the proposal.

    Without consent the Brief is untouched, so a mark left here would say the assistant wrote a
    document it was explicitly not allowed to write.
    """
    client, store, _ = make_client(
        tmp_path, director=PlanningDirector(turn(brief="A Brief nobody consented to."))
    )
    project = planning_project(store)

    client.post(TURN.format(project=project.id), json={"message": "no consent"})

    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.creative_brief == project.creative_brief
    assert marks(saved) == []


def test_suggest_video_marks_the_brief_it_wrote_with_no_turn_to_name(tmp_path):
    """The second machine writer, and the one with no thread: the id is honestly blank.

    Suggest Video is its own pass, not a turn of a conversation — there is no message, so there
    is no id, and inventing one would be a pointer into nothing.
    """
    client, store, _ = make_client(tmp_path, director=SuggestingDirector(suggestion()))
    project = suggestable_project(store, brief="The Director's first sketch.")

    assert client.post(SUGGEST.format(project=project.id)).status_code == 200

    saved = ProjectStore(tmp_path).get(project.id)
    assert marks(saved) == [(0, len(saved.creative_brief), "")]
    assert marked_text(saved) == [saved.creative_brief]


def test_a_chat_turn_that_rewrites_the_other_documents_leaves_the_marks_alone(tmp_path):
    """The fourth caller of `write_document`, which cannot write the Brief and must not mark it.

    `DIRECTOR_REPLACEABLE_DOCUMENTS` is derived from `DirectorResult`, which carries no Brief.
    Asserted rather than assumed, because the reconciliation now runs inside the writer that
    loop calls three times per reply.
    """
    client, store = make_client_with_director(tmp_path, RevisingDirector())
    project = marked_project(store, "Chat")

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "moodier", "apply_shots": True, "apply_documents": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.creative_brief == "Alpha beta. Gamma delta."
    assert marks(saved) == [(12, 24, "msg_one")]


def test_a_restore_reconciles_the_marks_against_the_version_it_brings_back(tmp_path):
    """The one Brief write that does not go through `write_document`, held to the same rule.

    A restore replaces the text under the marks. Leaving them where they were would not be stale
    metadata — it would be a claim that the assistant wrote whatever characters now sit at those
    offsets, which is the invented provenance this slice exists to prevent. Marks whose exact
    text is in the restored version keep it; the rest go.
    """
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = documented_project(store, "Restore")
    project.creative_brief = "Alpha beta. Gamma delta."
    project.creative_brief_previous = "Gamma delta. And a sentence the assistant never wrote."
    project.brief_attribution = [mark(0, 11, "msg_one"), mark(12, 24, "msg_two")]
    store.save(project)

    response = client.post(f"/api/projects/{project.id}/documents/creative_brief/restore")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.creative_brief == "Gamma delta. And a sentence the assistant never wrote."
    # "Alpha beta." is not in the restored version and its mark goes; "Gamma delta." is, and its
    # mark follows it to offset 0 — where it covers the same twelve characters it always did.
    assert marks(saved) == [(0, 12, "msg_two")]
    assert marked_text(saved) == ["Gamma delta."]


# ---------------------------------------------------------------------------------------------
# The generic PUT, both directions
# ---------------------------------------------------------------------------------------------


def test_the_full_project_put_can_neither_clear_nor_invent_attribution(tmp_path):
    """Acceptance 4, and the seventeenth instance of this route's one recorded hole.

    Both directions, and the second is the worse one. A body that *omits* the field — every
    client written before it existed, every hand-rolled call — arrives as `[]` and would strip
    the record of what the assistant wrote off a Brief whose text is still on the page. A body
    that *invents* ranges is planting authorship: it would tell a Director six months later that
    a paragraph they wrote themselves came out of a model, and there is nothing in the manifest
    afterwards that says otherwise.
    """
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = marked_project(store, "Full save")

    omitted = client.get(f"/api/projects/{project.id}").json()
    del omitted[ATTRIBUTION_FIELD]
    omitted["treatment"] = "Edited by a client that predates attribution."

    assert client.put(f"/api/projects/{project.id}", json=omitted).status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.treatment == "Edited by a client that predates attribution."
    assert marks(saved) == [(12, 24, "msg_one")]

    forged = client.get(f"/api/projects/{project.id}").json()
    forged[ATTRIBUTION_FIELD] = [
        {"start": 0, "end": 11, "message_id": "msg_forged"},
        {"start": 12, "end": 24, "message_id": "msg_forged"},
    ]

    assert client.put(f"/api/projects/{project.id}", json=forged).status_code == 200
    unforged = ProjectStore(tmp_path).get(project.id)
    assert marks(unforged) == [(12, 24, "msg_one")]
    # And the forgery reached nothing a Director can be shown: the served project is the stored
    # one, so a client that planted a mark cannot read its own plant back.
    assert client.get(f"/api/projects/{project.id}").json()[ATTRIBUTION_FIELD] == [
        {"start": 12, "end": 24, "message_id": "msg_one"}
    ]


def test_a_full_project_put_cannot_plant_marks_on_an_unattributed_brief(tmp_path):
    """The invent direction where there is nothing to compare against, which is the live case.

    Every project in the Director's library today has an empty list here. A guard written as
    *keep whatever is stored when the body differs* would look identical to this one and would
    let a body plant marks into an empty field, because there is no stored value to disagree
    with. Adoption is unconditional for exactly that reason.
    """
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = documented_project(store, "Unmarked")

    forged = client.get(f"/api/projects/{project.id}").json()
    forged[ATTRIBUTION_FIELD] = [{"start": 0, "end": 5, "message_id": "msg_forged"}]

    assert client.put(f"/api/projects/{project.id}", json=forged).status_code == 200
    assert ProjectStore(tmp_path).get(project.id).brief_attribution == []


def test_attribution_survives_reload_because_it_is_a_property_of_the_document(tmp_path):
    """Acceptance 5, asserted rather than assumed: through the manifest, not through memory.

    The bytes on disk are read directly as well as through the store, because a field that
    round-trips in Pydantic but is not serialised would pass every other test in this module.
    """
    client, store, _ = make_client(tmp_path, director=SuggestingDirector(suggestion()))
    project = suggestable_project(store, brief="The Director's first sketch.")
    client.post(SUGGEST.format(project=project.id))

    raw = json.loads(store.manifest_path(project.id).read_text(encoding="utf-8"))
    assert raw[ATTRIBUTION_FIELD] == [
        {"start": 0, "end": len(raw["creative_brief"]), "message_id": ""}
    ]
    assert marks(ProjectStore(tmp_path).get(project.id)) == [(0, len(raw["creative_brief"]), "")]


def test_a_manifest_written_before_the_field_existed_loads_and_behaves_unchanged(tmp_path):
    """Constraint 6: no ranges is a valid state, and it is the state of every existing project.

    The key is removed from the file on disk, which is exactly what every Brief in the Director's
    library has. It loads, it saves, it edits, and it behaves identically to a hand-typed Brief
    all the way through — reconciliation over an empty list is the identity, so a save leaves it
    empty rather than inventing a first mark.
    """
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = documented_project(store, "Legacy")
    path = store.manifest_path(project.id)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored.pop(ATTRIBUTION_FIELD) == []
    assert ATTRIBUTION_FIELD not in stored
    path.write_text(json.dumps(stored), encoding="utf-8")

    assert store.get(project.id).brief_attribution == []
    saved = client.put(
        f"/api/projects/{project.id}/documents",
        json=project_documents(store.get(project.id), creative_brief="Typed by hand, as always."),
    )

    assert saved.status_code == 200
    reloaded = ProjectStore(tmp_path).get(project.id)
    assert reloaded.creative_brief == "Typed by hand, as always."
    assert reloaded.brief_attribution == []


# ---------------------------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------------------------


def test_every_place_that_writes_a_creative_documents_text_is_one_that_reconciles():
    """**The guard that would have caught the false record this slice was built on top of.**

    `write_document`'s docstring has said *"the one place a creative document's text and its
    recovery slot are assigned"* since Slice E1, and the half about the text was never true:
    `restore_document` assigns it with `setattr(project, document, previous)` and does not call
    `write_document`. The claim was mine, and it is the shape Epic 11 named the most expensive
    kind of false record this repository produces — *a claim that an invariant is held stops the
    next reader looking for the place that holds it.* Slice C very nearly attached the
    reconciliation only where the claim said to, which would have left the restore swapping new
    text under old offsets: marks pointing at characters the assistant never wrote, and past the
    end of a shorter document. **Invented provenance, which AD-45 calls worse than none.**

    The sibling guard beside this one enumerates the writers of the *mark* and cannot see this: a
    function that assigns the text and no mark is invisible to it by construction, which is
    exactly the fifth writer that would reintroduce the defect. So this asserts the pair, in
    `test_every_writer_of_an_expansion_records_the_map_it_was_written_against`'s shape — the
    same sibling gap, the same remedy.

    Both writers assign through `setattr` with the document's *name* in a variable, so there is no
    attribute node to match on and the scan reads the call's source segment. That is looser than
    the mark scan and is stated rather than hidden: a text write built some third way —
    `Project(...)`, a dict splat, a `model_copy(update=...)` — is not seen.
    """

    def enclosing(node, parents, fallback):
        while node is not None:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return node.name
            node = parents.get(node)
        return f"<{fallback}>"

    documents = set(DOCUMENT_LABELS)
    writers = set()
    for path in package_modules():
        code = path.read_text(encoding="utf-8")
        tree = ast.parse(code)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            wrote = any(
                isinstance(target, ast.Attribute) and target.attr in documents
                for target in targets
            )
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setattr":
                segment = ast.get_source_segment(code, node) or ""
                # The slot and the mark are different fields with their own guards; this one is
                # about the document's own text, so their writes are not this test's business.
                if RECOVERY_SLOT_SUFFIX not in segment and ATTRIBUTION_FIELD not in segment:
                    wrote = wrote or "project, document," in " ".join(segment.split())
            if wrote:
                writers.add(enclosing(node, parents, module_name(path)))

    assert writers == {
        # The one writer, which reconciles because it is the one writer.
        "write_document",
        # The swap. It writes no new text — it puts a stored version back — so it cannot use
        # the shared writer, whose job is to fill the slot this one reads. It runs the same pure
        # function by hand, and `test_a_restore_reconciles_the_marks_against_the_version_it_brings_back`
        # is what proves it does.
        "restore_document",
    }, writers


def test_only_three_places_in_the_package_write_the_brief_s_attribution():
    """AD-45 asserted against the source: the server is the sole writer, and it is these three.

    The slot guard's shape, one field along (`test_suggest_video`'s
    `test_every_writer_of_a_creative_document_goes_through_the_one_capture`), and for the same
    reason: *provenance with two authorities is provenance with none*, so a second place deciding
    what is marked is the defect, and it has no symptom at the point it is added.

    The scan is over the **raw** source of every module in the package: a docstring cannot hold
    an assignment node, so prose is excluded by the parse itself. What it catches is an
    assignment to an attribute of this name, and any `setattr` whose call text mentions the field
    or the mapping's local. What it cannot see is stated rather than implied: a mutation of the
    list in place (`.append`) is not an assignment, and neither is a `Project(...)` construction.
    """

    def enclosing(node, parents, fallback):
        while node is not None:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return node.name
            node = parents.get(node)
        return f"<{fallback}>"

    writers = set()
    for path in package_modules():
        code = path.read_text(encoding="utf-8")
        tree = ast.parse(code)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            marked = any(
                isinstance(target, ast.Attribute) and target.attr == ATTRIBUTION_FIELD
                for target in targets
            )
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setattr":
                segment = ast.get_source_segment(code, node) or ""
                marked = marked or ATTRIBUTION_FIELD in segment or "attributed" in segment
            if marked:
                writers.add(enclosing(node, parents, module_name(path)))

    assert writers == {
        # The one reconciliation, run for the Director's save, the planning turn and Suggest
        # Video alike — which is the whole reason the single document writer was extracted.
        "write_document",
        # The swap, which replaces the text under the marks without writing new text, so it
        # reconciles by hand against the version it brings back.
        "restore_document",
        # `replace_project` taking the stored marks off a client body, which writes no provenance.
        "replace_project",
    }, writers


def test_attribution_is_withheld_from_every_director_prompt(tmp_path):
    """A newly declared `Project` field is in the Director's dump the moment it exists.

    Unlike `Song`, `Shot` and `Asset`, `Project`'s own fields are not classified — nothing raises
    at import for an unclassified one — so the decision has to be made here, in
    `DIRECTOR_CONTEXT_EXCLUDE`, or a list of integer spans joins every chat turn by default.
    Withheld on the never-been-in grounds `SHOT_DIRECTOR_WITHHELD` sets: the model is handed the
    Brief itself, and this keeps every prompt byte-identical to what it was before the field.
    """
    assert DIRECTOR_CONTEXT_EXCLUDE[ATTRIBUTION_FIELD] is True

    director = PlanningDirector(turn())
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store)
    project.brief_attribution = [mark(0, 11, "msg_one")]
    store.save(project)

    client.post(TURN.format(project=project.id), json={"message": "what do you see?"})

    # What actually reached the model, not what a dump built here would have contained.
    context = director.contexts[-1]
    assert ATTRIBUTION_FIELD not in context
    assert context["creative_brief"] == project.creative_brief
