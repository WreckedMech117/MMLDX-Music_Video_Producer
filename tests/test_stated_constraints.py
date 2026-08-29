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

**The headline: of the twenty-two, ~~seven~~ eight were asserted before this pass, three are
closed by the leaf-module guard added beside it, and ~~twelve~~ eleven remain unenforced and are
named as such.** Nothing here tried to close the twelve — item 23 asked for the list, not the
fixes.

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
        "one entry per transition the export composed, and one per transition it refused",
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
        "empty one — so the slot can no longer go false in either direction in silence.",
    ),
    Constraint(
        "models.py / app.py, `SHOT_PLAN_CONTENT_FIELDS` and `_withheld_fields`",
        "every declared field of the model belongs to exactly one of the two classifications",
        "test_every_manifest_write_is_classified",
        "The strongest form in the repository: `_withheld_fields` refuses to build an exclusion at "
        "all unless the partition is total, so an unclassified new field is an import-time error "
        "and the application will not start. Enforced by the code, then pinned by a test.",
    ),
    # --- app.py -------------------------------------------------------------------------------
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
    **eleven** unenforced. If a later change closes one of the remaining eleven, this fails and
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

    assert len(CONSTRAINTS) == 22
    assert len(by_new_guards) == 3
    assert len(asserted) - len(by_new_guards) == 8
    assert len(CONSTRAINTS) - len(asserted) == 11


def test_no_constraint_is_listed_twice():
    """Two entries for one rule would double-count the split above."""
    seen = [(c.where, c.claim) for c in CONSTRAINTS]
    assert len(set(seen)) == len(seen)
