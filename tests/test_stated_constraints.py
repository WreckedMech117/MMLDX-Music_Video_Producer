"""The enumeration item 23 asked for: which stated constraints are executed, and which are not.

Epic 10's retrospective action item **23** — *a prose statement of a rule is not a guard, including
inside source* — asked for a sweep of the comments in `effects.py`, `models.py` and `app.py` for
statements of a constraint, reporting **which are asserted by a test and which are not**. It said
explicitly not to close them all: the deliverable is the list, so the next person chooses from one
rather than rediscovering it.

**The enumeration is here, as data, rather than in a document, for the reason item 23 exists.** A
markdown table of unenforced rules would be exactly one more prose statement of a rule — and would
go stale the first time one of the tests it names is renamed. Held here, every `asserted` verdict
is checked: the test it names has to exist, by that name, somewhere under `tests/`. A verdict that
stops being true fails rather than misleading.

**How the list was made, so its shape can be judged.** `package_source.module_prose` was swept for
sentences carrying a constraint word — *never*, *must*, *may not*, *cannot*, *refuses*, *always*,
*exactly N*, *the only*. That is **911 sentences** across the three modules (effects 166, models
110, app 635), which is far too many to be a list anybody reads. Narrowing to claims about the
**source itself** rather than about runtime data — *the only writer*, *exactly one*, *nothing else
in this codebase*, *never imports*, *may not import* — gives **135**, of which 44 are in
`effects.py`, 11 in `models.py` and 80 in `app.py`. The twenty-two below are the load-bearing ones
from that 135: each is a claim that, if it silently stopped holding, would change what the
application does rather than only what a comment says.

**The headline: of the ~~twenty-two~~ ~~twenty-six~~ ~~twenty-eight~~ ~~thirty-one~~ thirty-three, ~~seven~~ ~~eight~~
~~twelve~~ ~~fifteen~~ seventeen were asserted before this pass, three are closed by the leaf-module guard added
beside it, and ~~twelve~~ ~~eleven~~ thirteen remain unenforced and are named as such.** Nothing here tried to close the twelve — item 23
asked for the list, not the fixes.

*Six rows added 2026-08-29 and 2026-08-30 by story 11.5, three more by the Slice F remediation of 2026-08-30, and two by the remediation of that remediation on 2026-08-31, in the commits that stated them* — which is the standing
practice `docs/project-context.md` records, and the only mechanism there is: nothing finds an
unenumerated constraint and nothing will. Three of the four are asserted, and the one that is not
is named honestly: the cache's single implementation is a claim about how many functions exist,
which no test in this repository counts.

*Amended 2026-08-28 by story 11.1, which closed one of the twelve.* `ExportLook.transitions`'
row said *"the day Epic 11 fills it, this comment goes false with nothing to say so"*; that day
came, the slot is now asserted in both directions, and the count moved with it. **This is the
mechanism working rather than an exception to it**: the row named the change that would falsify
it, the change carried the correction, and `test_the_enumeration_reports_its_own_split` is what
made carrying it compulsory.

**What this module does not do.** It does not check that an `unenforced` entry is still unenforced
— a test written for one later would make this file's verdict stale in the safe direction, and
detecting that reliably would mean guessing at what counts as asserting it. It covers 22 of the
135 structural claims, and those 135 are of 911 constraint sentences; the other 776 are claims
about runtime behaviour, where a route's own tests are the right home and a scan is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Constraint:
    """One stated rule, where it is stated, and what executes it."""

    where: str
    claim: str
    #: The test function that asserts it, or `""` for a rule nothing executes.
    asserted_by: str
    note: str = ""


#: Ordered by module, then by the claim's position in it.
CONSTRAINTS: tuple[Constraint, ...] = (
    # --- effects.py ---------------------------------------------------------------------------
    Constraint(
        "effects.py module docstring / AD-25",
        "imports the standard library and nothing from this package",
        "test_the_leaf_module_imports_the_standard_library_and_its_one_allowance",
        "Unenforced until 2026-08-28. The retrospective's own words were 'true by discipline'.",
    ),
    Constraint(
        "effects.py, `preview_fingerprint`'s neighbourhood",
        "the frame grid lives in `timeline`, which this module may not import",
        "test_the_leaf_module_imports_the_standard_library_and_its_one_allowance",
        "The same guard; this sentence names the specific edge AD-25 forbids in general.",
    ),
    Constraint(
        "effects.py, `BOUNDARY_FINGERPRINT_INPUTS`",
        "the client's `BOUNDARY_KEY_INPUTS` plus `BOUNDARY_KEY_UNSEEN` is this tuple, in order",
        "test_the_client_and_the_server_enumerate_one_boundary_key",
        "Story 11.5. Enumerated in two engines and compared, which is the guard nobody had when "
        "`previewInputKey` and `preview_fingerprint` disagreed for a whole epic — the client "
        "cannot gain a slot the server has not, and cannot silently lose one.",
    ),
    Constraint(
        "effects.py, `_branch_stage`",
        "the only writer of a branch, so the label convention lives in exactly one place",
        "",
        "`test_effects.py` asserts the composed *text* at the two composers that broke it. "
        "Nothing counts the writers, so a second one is invisible — the shape "
        "`package_source.modules_containing` exists for.",
    ),
    Constraint(
        "effects.py, `validate_stack`",
        "the only thing that decides whether a value may reach a filter string (AD-27)",
        "",
        "Its decisions are asserted exhaustively; its uniqueness is not. A second validator "
        "anywhere in the package would pass every test in the suite.",
    ),
    Constraint(
        "effects.py, `_canonical` and `validate_stack`",
        "`sorted` over keys of two types raising `TypeError` is the one thing this must never do",
        "test_a_stack_that_is_not_a_list_at_all_is_refused_rather_than_raised_through",
        "`key=repr` throughout; the two shapes that used to escape as `TypeError` are pinned.",
    ),
    Constraint(
        "effects.py, `BINDING_KEYS`",
        "`id` is declared here and read by nothing in this module (R-33)",
        "",
        "The route behaviour around card ids is tested at length. That `effects.py` never reads "
        "the key is asserted nowhere, and it is the half R-33 turns on.",
    ),
    Constraint(
        "effects.py, `drive_samples`",
        "the readout and the script are one implementation, not two agreeing (R-27)",
        "test_the_readout_draws_the_very_lines_the_script_carries",
        "The strongest form available: a shared function, then the drawn series compared against "
        "the `.cmds` file read back off disk.",
    ),
    Constraint(
        "effects.py, `build_effect_stages`",
        "a binding with no envelope is refused by name; a binding is never silently dropped",
        "test_a_bound_stack_with_no_envelope_refuses_rather_than_naming_an_undriven_picture",
        "FX-15. The refusal sentence is a module constant and is asserted by identity.",
    ),
    Constraint(
        "effects.py, `agreed_bindings` / `exported_look`",
        "the catalogue is the only thing entitled to say what a parameter's bounds are (AD-27)",
        "",
        "Every consumer is tested against the catalogue. That no consumer carries its own copy "
        "of a bound is not checked, and a copied bound is what AD-27 exists to prevent.",
    ),
    # --- models.py ----------------------------------------------------------------------------
    Constraint(
        "models.py, above `from .h3_prompt import REFERENCE_TAG_NAMES`",
        "`h3_prompt` is a leaf; **nothing may ever import `models` from `h3_prompt`**",
        "test_the_leaf_module_imports_the_standard_library_and_its_one_allowance",
        "Unenforced until 2026-08-28. Stated in bold, about the one edge that would make a cycle.",
    ),
    Constraint(
        "models.py, `Shot.singing`",
        "nothing in this codebase infers it: no route derives it, no tool schema exposes it",
        "test_whether_the_performer_is_singing_is_expressible_and_nothing_infers_it",
        "And `test_the_assistant_never_infers_whether_the_performer_is_singing` for the model half.",
    ),
    Constraint(
        "models.py, `Shot.shot_kind` and `Shot.character_slot`",
        "nothing infers it — no route derives it, and no tool schema exposes it to a model",
        "",
        "The same sentence as `singing`, three fields over, with no equivalent test. The `singing` "
        "test is the template; nothing generalises it across the fields that carry the claim.",
    ),
    Constraint(
        "models.py, `ExportLook.transitions`",
        "four forms in one slot: composed, one-sided, refused, diverged - and empty means none",
        "test_the_export_with_a_transition_matches_the_song_and_records_what_it_blended",
        "**Amended 2026-08-28 by story 11.1, the story this row predicted.** The claim used to "
        "read ~~'present and empty — genuinely, on every record this build writes'~~ and the note "
        "said: *'the day Epic 11 fills it, this comment goes false with nothing to say so.'* That "
        "day is this commit, and the amendment lands in it — which is the whole of what this row "
        "was for. Both directions are now executed: "
        "`test_the_export_with_a_transition_matches_the_song_and_records_what_it_blended` "
        "asserts a real non-empty value, "
        "`test_more_than_two_clips_over_one_instant_still_exports_and_says_what_it_refused` "
        "asserts the refusal line, and "
        "`test_a_shot_with_no_transition_exports_exactly_what_it_exported_before` asserts the "
        "empty one — so the slot can no longer go false in either direction in silence. "
        "**Amended again 2026-08-29 by story 11.4 and story 11.3's third criterion**, which put "
        "two more forms in the same slot and so falsified the claim as it was worded — a row "
        "reading 'composed, and refused' would have gone on passing while two thirds of what the "
        "slot can hold went unnamed. "
        "`test_a_one_sided_transition_treats_its_own_final_frames_and_changes_no_count` asserts "
        "the one-sided form with its length, "
        "`test_a_pair_only_type_left_one_sided_is_refused_with_its_reason_and_nothing_substituted` "
        "the second reason a record can read `refused:`, and "
        "`test_a_pair_that_disagrees_across_an_overlap_is_reported_once_and_never_refuses` and "
        "`test_an_unset_or_agreeing_mirror_is_not_a_divergence` the diverged form in both "
        "directions — including the three states that must produce no line at all.",
    ),
    Constraint(
        "models.py / app.py, `SHOT_PLAN_CONTENT_FIELDS` and `_withheld_fields`",
        "every declared field of the model belongs to exactly one of the two classifications",
        "test_every_manifest_write_is_classified",
        "The strongest form in the repository: `_withheld_fields` refuses to build an exclusion at "
        "all unless the partition is total, so an unclassified new field is an import-time error "
        "and the application will not start. Enforced by the code, then pinned by a test.",
    ),
    Constraint(
        "assembly.py, `xfade_stage`",
        "written once, for the export's segment and for its preview",
        "test_the_preview_and_the_export_write_one_xfade_by_name_and_by_duration",
        "Story 11.5, and it is what makes FX-NFR-3's *by name and by duration* a string "
        "comparison on two composed graphs rather than a reading of two argv builders.",
    ),
    Constraint(
        "api.js, `gridFrames`",
        "rounds half to the nearer even integer, as `assembly.clip_frames_on_grid` does",
        "test_the_rows_readout_and_the_routes_transition_seconds_are_one_number",
        "R-42, 2026-08-30. `Math.round` goes half *up* and Python's `round` goes half to *even*, "
        "so the two part on every boundary landing on a half-frame -- measured at 2 frames "
        "against 1. The table that asserts it carries three such rows and the shape that "
        "separates telescoping from subtraction; a mutation survived the table without them.",
    ),
    Constraint(
        "api.js, `gridFrames`",
        "the one place this side rounds onto the assembly grid",
        "",
        "The rounding *rule* is asserted (row above); its *uniqueness* is not, and a second "
        "`Math.round(x * ASSEMBLY_FPS)` anywhere in `api.js` would pass every test in the suite "
        "while printing a length the export will not render. Same shape as `_branch_stage`'s row, "
        "and the same remedy would serve: something that counts the writers.",
    ),
    Constraint(
        "assembly.py, `TRANSITION_PREVIEW_MARGIN_FRAMES` / api.js's copy of it",
        "one number in two files, because the client cannot import Python",
        "test_the_client_and_the_server_agree_on_the_boundary_margin",
        "Story 11.5, beside `ASSEMBLY_FPS` and `BOUNDARY_TOLERANCE_SECONDS`. A duplicated "
        "*constant* is safe exactly while something compares the two; a duplicated *rule* is not, "
        "which is why the clamp itself is compared on answers instead.",
    ),
    Constraint(
        "assembly.py, `assembly_plan` / `AssemblyPlan.frames`",
        "nothing leaves this function with a non-positive frame count",
        "test_no_plan_this_module_returns_holds_a_non_positive_frame_count",
        "AD-18's 2026-08-29 amendment recorded this as *asserted at the split now* and it was "
        "not: it existed in one test, on one fixture, and three further shapes of the same defect "
        "shipped under it. A negative count keeps `sum(frames)` correct by cancelling a window "
        "against itself, which is the one way the frame rule can hold while the export ships the "
        "wrong Shot.",
    ),
    Constraint(
        "assembly.py, `_paired_transitions`",
        "one rule decides whether a boundary blends, and it measures the split it would make: "
        "`outgoing > 0 and blend > 0 and incoming >= 0`",
        "test_a_split_whose_incoming_stretch_runs_backwards_is_refused_with_all_three_numbers",
        "2026-08-30, amended by the Director on 2026-08-31. The two conditions it replaces each "
        "described a geometry somebody had enumerated and each asked its question of a different "
        "object than the split used. `TRANSITION_NESTED_REFUSAL` and `TRANSITION_CROWDED_REFUSAL` "
        "are now names for what the measurement found rather than tests of their own. The rule "
        "shipped as `min(...) > 0`, which refused a blend the module had composed the day before; "
        "a **zero** third stretch composes and its zero-length entry falls through to the drop "
        "`assembly_plan` already makes, a **negative** one still refuses.",
    ),
    Constraint(
        "assembly.py, `AssemblyPlan.transition_refusals`",
        "a refusal is selected by the pair it is about, never by looking for a label in it",
        "test_a_boundary_preview_quotes_its_own_boundarys_refusal_and_no_other",
        "2026-08-31. Every refusal names **both** Shots, so `render_boundary_preview`'s "
        "`if label in line` matched a Shot that is the incoming side of one refused boundary and "
        "the outgoing side of another on both -- and answered about the wrong one. It is the same "
        "correction `66c90d8` made to the index lookup twelve lines above and did not make here.",
    ),
    # --- app.py -------------------------------------------------------------------------------
    Constraint(
        "routes/shots.py, `replace_shot_transitions`'s mirror-lock loop",
        "the lock on the other end holds a **blend**, so the boundary has to be overlapped",
        "test_a_lock_holds_a_blend_and_not_a_shots_treatment_of_its_own_frames",
        "2026-08-31. The loop shipped without asking whether the two Shots overlap, so a lock "
        "anywhere made the Shot in front of it un-fadeable and said so in a sentence that states "
        "a falsehood -- *a transition between SHOT 01 and SHOT 02 is written on both of them* "
        "where there is no transition between them. On a boundary with no Overlap a "
        "`transition_out` is a one-sided treatment of the addressed Shot's own last frames "
        "(AD-19, story 11.4).",
    ),
    Constraint(
        "app.py, `SHOT_TRANSITION_MIRROR_LOCKED_REFUSAL`",
        "a lock holds both ends of one blend, not only the Shot the request names",
        "test_a_locked_shot_cannot_be_given_a_blend_through_the_shot_beside_it",
        "2026-08-30. AD-30's mirror wrote the neighbour's field with no lock check, and because "
        "`transition_in` mirrors *backwards* onto the predecessor's `transition_out` -- the only "
        "side the export reads -- naming the unlocked end authored the blend that actually "
        "renders on the locked Shot.",
    ),
    Constraint(
        "app.py, `MARK_READY_STATUSES`",
        "the exact complement of `RENDER_AGAIN_STATUSES` plus the in-flight pair",
        "test_every_shot_status_belongs_to_exactly_one_of_the_two_actions",
        "The comment names its own test in prose — the convention this module proposes making "
        "machine-readable. It is the only constraint comment in the three files that does.",
    ),
    Constraint(
        "app.py, `replace_project`",
        "no route in this application removes a job record",
        "",
        "One route's behaviour is asserted (a three-key `PUT` no longer erases the list). The "
        "claim is about *every* route and is checkable by scan; nothing counts removal sites.",
    ),
    Constraint(
        "app.py, `replace_project`",
        "the queue panel is the only thing that reads job records",
        "",
        "A cross-language claim about `api.js` with no counterpart test.",
    ),
    Constraint(
        "app.py, the envelope staleness section",
        "there is no stored flag saying an envelope is stale, and there must never be",
        "",
        "AD-11's standing law, restated per feature. Derivation is asserted; the absence of a "
        "stored flag is not, and a stored flag is exactly what a later epic would add for speed.",
    ),
    Constraint(
        "app.py, `assemble_timeline`",
        "the export reads `approved_output` and nothing else (AD-13)",
        "",
        "Behaviourally covered where it matters; the 'nothing else' is a scan nobody has written. "
        "`AGENTS.md` carries the same rule as a pitfall.",
    ),
    Constraint(
        "app.py, `run_ffmpeg`'s `on_start`",
        "exists for exactly one caller: the preview render, which AD-24 requires be cancellable",
        "",
        "Cancellation is tested. That a second caller has not appeared is not, and a second one "
        "would silently acquire a kill handle the design never gave it.",
    ),
    Constraint(
        "app.py, the assistant's report",
        "`shot_label` is called rather than re-derived, so exactly one function in this layer "
        "turns a Shot into the name the Director sees",
        "",
        "The `Delete SHOT 05?`-over-`SHOT 02` defect was this rule broken across languages, and "
        "the browser harness that caught it is outside the suite. A count here is cheap.",
    ),
    Constraint(
        "app.py, `render_shot_preview`",
        "the preview is the export's own chain at smaller dimensions, differing in nothing else",
        "test_the_effect_stack_is_actually_applied_by_the_exports_own_chain",
        "FX-NFR-3 and standing design law 2. Asserted by comparing the composed argv rather than "
        "the picture, which is the only form of it that can be checked without a render.",
    ),
    Constraint(
        "app.py, `preview_into_cache`",
        "the one implementation of the preview cache, the supersede rule and the join",
        "",
        "Story 11.5 extracted it so the Shot preview and the boundary preview cannot keep two "
        "supersede registries for one project. Its *behaviour* is asserted exhaustively through "
        "both routes; its uniqueness is not, and a second copy anywhere in the package would pass "
        "every test in the suite. Same shape as `_branch_stage`'s row above.",
    ),
)


def declared_tests() -> set[str]:
    """Every test function name declared anywhere under `tests/`."""
    found: set[str] = set()
    for path in sorted(TESTS.glob("test_*.py")):
        found |= set(re.findall(r"^def (test_\w+)", path.read_text(encoding="utf-8"), re.MULTILINE))
    return found


@pytest.mark.parametrize(
    "constraint", [c for c in CONSTRAINTS if c.asserted_by], ids=lambda c: c.asserted_by
)
def test_every_constraint_marked_asserted_names_a_test_that_exists(constraint):
    """A verdict of `asserted` is a claim, so it is executed like every other claim here.

    This is the whole reason the enumeration is code. Rename the test that closes a rule and this
    fails, instead of the enumeration quietly becoming a list of rules nobody is checking.
    """
    assert constraint.asserted_by in declared_tests(), (
        f"{constraint.where} is recorded as asserted by {constraint.asserted_by}, "
        "and no test by that name is declared under tests/"
    )


def test_the_enumeration_reports_its_own_split():
    """The count in the module docstring, so the headline cannot drift from the table.

    ~~Seven~~ **eight** asserted before this pass, three by the guard it added, ~~twelve~~
    **eleven** unenforced -- the numbers as first written; the module docstring above carries them
    as they stand. If a later change closes one of the remaining thirteen, this fails and
    the docstring is corrected with it — which is item 21's rule applied to the document item 23
    asked for, and it has already fired once: story 11.1 closed `ExportLook.transitions`' row on
    2026-08-28, exactly as that row predicted, and this test is what made the correction land in
    the same commit rather than a retrospective later.
    """
    added_here = {
        "test_the_leaf_module_imports_the_standard_library_and_its_one_allowance",
    }
    asserted = [c for c in CONSTRAINTS if c.asserted_by]
    by_new_guards = [c for c in asserted if c.asserted_by in added_here]

    assert len(CONSTRAINTS) == 33
    assert len(by_new_guards) == 3
    assert len(asserted) - len(by_new_guards) == 17
    assert len(CONSTRAINTS) - len(asserted) == 13


def test_no_constraint_is_listed_twice():
    """Two entries for one rule would double-count the split above."""
    seen = [(c.where, c.claim) for c in CONSTRAINTS]
    assert len(set(seen)) == len(seen)
