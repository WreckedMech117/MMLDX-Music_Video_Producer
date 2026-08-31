"""Two scans for the records this repository writes about itself and then falsifies.

Epic 10's retrospective action item **21**: *a commit that changes what a document describes must
correct that document in the same commit.* It was written as a rule, and the rule was broken by
the commit immediately after it. This module is the half of that rule a machine can hold.

**What made the rule worth mechanising is that every instance was written by a careful author, on
purpose, about a thing that was genuinely absent at the time.** Nobody was being sloppy. `ad67a14`
wrote *"there is no generator, no script text, and no caller"* about `sendcmd` in the commit that
added `sendcmd_script`, and indexed the rulings as `R-8…R-28` in the commit that added R-29 and
R-30. `effects.py` and `models.py` each said a field *"does not exist on any model yet"* about a
field a shipped test already asserts non-empty. So the scan cannot simply hunt for negative
sentences: it has to tell **absent then, present now** from **absent and still absent**, because
`transition` is still genuinely absent and its sentences have to keep passing.

**The discriminator, and it is the whole design.** A claim that a name does not exist yet is false
exactly when that name is now in the package's *code*. `package_source` already separates a
module's code from its prose — `module_code` blanks the prose, `module_prose` blanks the code, and
they are complements over the same character grid. Before `ad67a14`, `sendcmd` appeared in
`effects.py` only inside a docstring and a comment; that is what its own sentence said (*"grep the
module and you get two lines of prose"*). After it, `sendcmd` is in the code. The same test run
over the same sentence gives opposite answers on either side of the commit, which is what a guard
for this needs and what a plain grep could never have.

**What this cannot see, stated rather than implied**, because an overstated gate is worse than a
missing one:

- It reads **prose**: `#` comments, docstrings, and markdown. A claim made in a variable name, a
  test's docstring (test files are not scanned), or either JavaScript asset is invisible to it.
- The existence test is **substring containment in the package's Python code**. A name that is
  also an ordinary English word, or a fragment of a longer identifier, reads as present. That is
  deliberate — the opposite error, missing a real falsification, is the one this epic paid for —
  and it is why the sentence patterns are narrow.
- It only fires on claims phrased as a **horizon**: *yet*, *planned*, *to be written*, *Epic N*.
  A flatly false statement with no horizon (*"nothing calls this"*) is not caught. Those are the
  larger class and item 23 is where they are enumerated.
- Text struck through with `~~…~~` is **exempt**, because that is this repository's own mark for
  a claim recorded as no longer true. A correction that keeps the false sentence visible is the
  idiom here and must stay possible.
- `docs/DEVELOPMENT-LOG.md` and `_bmad-output/implementation-artifacts/` are **excluded**: both
  are dated records, and a retrospective quoting a false sentence as evidence is doing its job.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from package_source import PACKAGE, module_name, module_prose, package_code, package_modules

REPO = PACKAGE.parent.parent

#: The documents that describe this application as it stands. A new file under either root joins
#: the scan without anyone remembering to add it, which is the failure mode of every curated list
#: in this repository — `docs/OPERATIONS.md`'s harness list lost three entries that way.
DOC_ROOTS = (REPO / "docs", REPO / "_bmad-output" / "planning-artifacts")

#: Loose files at the repository root that are read as current descriptions.
DOC_FILES = (REPO / "AGENTS.md", REPO / "README.md")

#: Dated records, exempt by kind rather than by convenience. A log entry and a retrospective are
#: both *snapshots*: `DEVELOPMENT-LOG.md` says so in its own opening blockquote, and a
#: retrospective that could not quote the false sentence it found could not report it.
EXCLUDED_DOCS = (REPO / "docs" / "DEVELOPMENT-LOG.md",)


def documents() -> list[Path]:
    """Every markdown file in the corpus, in a stable order."""
    found: list[Path] = []
    for root in DOC_ROOTS:
        found.extend(sorted(root.rglob("*.md")))
    found.extend(path for path in DOC_FILES if path.exists())
    return [path for path in found if path not in EXCLUDED_DOCS]


def where(path: Path) -> str:
    """A path as this repository writes them: relative to the root, forward slashes."""
    return path.relative_to(REPO).as_posix()


# --- the absence scan ------------------------------------------------------------------------

#: A claim that a named thing is not in the code. Every alternative here was taken from a sentence
#: this repository actually shipped false; nothing is here on speculation.
#:
#: `is not implemented` is deliberately **absent** from this set and `is not yet implemented` is
#: present. `docs/ROADMAP.md` says *"`flagged` state is a later decision and is not implemented
#: here"* — scoped by *here*, true of the readiness report, and `flagged` is implemented elsewhere.
#: A pattern that could not tell those apart would have made this scan noise on the day it landed.
ABSENCE = re.compile(
    r"do(?:es)?\s+not\s+(?:yet\s+)?exist"
    r"|(?:is|are)\s+not\s+yet\s+(?:written|implemented|built|generated)"
    r"|(?:has|have)\s+not\s+(?:yet\s+)?been\s+(?:written|implemented|built|generated)"
    r"|there\s+(?:is|are)\s+no\s+(?:generator|caller|callers|implementation|script\s+text)"
    r"|lines?\s+of\s+\*?prose\*?"
    r"|nowhere\s+in\s+(?:the|this)\s+(?:module|package|codebase|application)",
    re.IGNORECASE,
)

#: The horizon that makes a claim of absence a claim with an expiry date. Required somewhere in
#: the surrounding block, not in the sentence: `ad67a14`'s worst instance put *"Planned — Epic 10
#: (Slice E)"* at the head of a table cell and *"There is no generator"* at its foot.
HORIZON = re.compile(r"\byet\b|\bplanned\b|\bto be written\b|\bEpic \d+\b", re.IGNORECASE)

BACKTICKED = re.compile(r"`([^`\n]{1,60})`")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")

#: How far either side of the absence phrase a backticked name still counts as its subject —
#: the gap between the two, not a raw character window, so a name is never taken half-quoted.
#: **Both numbers were measured against the sentences this repository actually shipped false, not
#: chosen.** In AD-25's struck row `sendcmd` sits 17 characters before *"lines of prose"*, while
#: `lut_file_argument` — a name that was there all along and is not what the sentence is about —
#: sits 32 characters after it: an `AFTER` wide enough to take the second would have failed that
#: document while its claim was still true. `BEFORE` is the larger of the two because a coordinated
#: subject pushes the first name away from the verb — *"`bindings` (Epic 10) and `transition`
#: (Epic 11) do not exist on any model yet"* leaves 36 characters between `bindings` and *"do
#: not"*, and naming only the second half of a two-name claim is naming the half that is still
#: true. Widened to 80 over the whole corpus without finding anything else, so 48 is not a
#: threshold anything currently sits near.
BEFORE, AFTER = 48, 24

#: Claims about the *shape* of a name rather than a name, which no scan of identifiers can judge.
#: Nothing is on this list today; it exists so the next exemption is written down with a reason
#: beside it rather than by widening a regex until the failure goes away.
ALLOWED: frozenset[tuple[str, str]] = frozenset()


def _blocks(text: str):
    """Paragraphs, and markdown table cells within them — the unit a horizon is looked for in."""
    for paragraph in re.split(r"\n\s*\n", text):
        for cell in paragraph.split("|"):
            if cell.strip():
                yield cell


def _without_struck(text: str) -> str:
    """`~~…~~` removed: this repository's mark for a claim it has already recorded as false."""
    return re.sub(r"~~.*?~~", " ", text, flags=re.DOTALL)


def stale_absence_claims(label: str, text: str) -> list[tuple[str, str, str]]:
    """Every `(label, name, sentence)` in `text` claiming a name is absent that the code now has."""
    found: list[tuple[str, str, str]] = []
    code = package_code()
    for block in _blocks(_without_struck(text)):
        flat = " ".join(block.split())
        if not HORIZON.search(flat):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", flat):
            quoted = list(BACKTICKED.finditer(sentence))
            for match in ABSENCE.finditer(sentence):
                for token in quoted:
                    if token.end() <= match.start():
                        gap = match.start() - token.end()
                        near = gap <= BEFORE
                    elif token.start() >= match.end():
                        near = token.start() - match.end() <= AFTER
                    else:
                        near = True
                    name = token.group(1).strip().removesuffix("'s").strip()
                    if not near or not IDENTIFIER.match(name) or (label, name) in ALLOWED:
                        continue
                    if name in code:
                        found.append((label, name, sentence))
    return found


def test_the_absence_scan_can_tell_absent_then_from_absent_still():
    """The positive control. Without it a broken regex reads as a clean repository.

    Both halves are the epic's own sentence, one word apart. `sendcmd_script` is in the code;
    `xfade` is Epic 11's and is in nothing at all.
    """
    false_now = "Planned — Epic 11. There is no generator for `sendcmd_script` and no caller."
    still_true = "Planned — Epic 11. There is no generator for `zzz_never_a_symbol_here` and no caller."

    assert [name for _, name, _ in stale_absence_claims("probe", false_now)] == ["sendcmd_script"]
    assert stale_absence_claims("probe", still_true) == []

    #: And a struck claim is exempt, because correcting one in place is how this repository
    #: records that it was wrong. Strike the same sentence and the scan lets it stand.
    struck = "Planned — Epic 11. ~~There is no generator for `sendcmd_script` and no caller.~~"
    assert stale_absence_claims("probe", struck) == []


def test_no_document_says_a_name_is_absent_that_the_package_now_has():
    """Item 21, over every markdown document that describes this application as it stands."""
    found: list[tuple[str, str, str]] = []
    for path in documents():
        found += stale_absence_claims(where(path), path.read_text(encoding="utf-8"))
    assert not found, "\n".join(
        f"{label}: `{name}` is in the package's code, and this says it is not yet:\n    {sentence}"
        for label, name, sentence in found
    )


def test_no_comment_says_a_name_is_absent_that_the_package_now_has():
    """The same scan over the package's own comments and docstrings.

    Three of Epic 10's nine false records were **in source**, which is the half a documentation
    audit never reaches. `module_prose` is exactly the text a reader of the file sees and the
    interpreter does not.
    """
    found: list[tuple[str, str, str]] = []
    for path in package_modules():
        found += stale_absence_claims(module_name(path), module_prose(path))
    assert not found, "\n".join(
        f"{label}: `{name}` is in the package's code, and this says it is not yet:\n    {sentence}"
        for label, name, sentence in found
    )


# --- the range scan --------------------------------------------------------------------------

#: `## R-30 — …`, `### AD-25 — …`: where an id is *defined*, as opposed to cited.
DEFINITION = re.compile(r"^#{1,6}\s+([A-Z][A-Z-]{0,7})-(\d+)\b", re.MULTILINE)

#: `R-8…R-28`, `AD-16…AD-31`, `FX-1..FX-25`, `R-25 to R-28`, `TP-3 – TP-5`.
CITATION = re.compile(
    r"\b([A-Z][A-Z-]{0,7})-(\d+)\s*(?:…|\.\.\.|\.\.|–|—|-{1,2}|to)\s*(?:\1-)?(\d+)\b"
)


def defined_id_ranges() -> dict[tuple[str, str], tuple[int, int]]:
    """`(prefix, document) -> (lowest, highest)` for every id the planning set declares."""
    seen: dict[tuple[str, str], list[int]] = {}
    for path in documents():
        for prefix, number in DEFINITION.findall(path.read_text(encoding="utf-8")):
            seen.setdefault((prefix, where(path)), []).append(int(number))
    return {key: (min(numbers), max(numbers)) for key, numbers in seen.items()}


#: A path fragment, backticked, in the prose beside a citation: `effects-director-rulings-…md`,
#: or the folder `architecture/architecture-MusicVideoProducer-effects-2026-08-21/`.
PATH_FRAGMENT = re.compile(r"`([A-Za-z0-9._/-]{6,}?)/?`")

#: How far in front of a range citation the document it indexes may be named. `BUILD-HANDOFF.md`'s
#: index row is the shape this is measured on: `` `…rulings-2026-08-24.md` (~~R-8…R-28~~ R-8…R-32) ``
#: names the file immediately before the bracket, and the AD range names its folder about eighty
#: characters ahead of it across a `·` separator.
NAMED_WITHIN = 200


def stale_range_citations(label: str, text: str) -> list[str]:
    """Every range in `text` that indexes a named document from its first id and stops short."""
    ranges = defined_id_ranges()
    live = _without_struck(text)
    stale: list[str] = []
    for citation in CITATION.finditer(live):
        prefix, first, last = citation.group(1), int(citation.group(2)), int(citation.group(3))
        lead = live[max(0, citation.start() - NAMED_WITHIN) : citation.start()]
        #: Nearest name wins. `BUILD-HANDOFF.md`'s index row names four documents in one line,
        #: each followed by its own range; matching all of them at once would exempt every one.
        for fragment in reversed(PATH_FRAGMENT.findall(lead)):
            named = {
                document: (lowest, highest)
                for (declared, document), (lowest, highest) in ranges.items()
                if declared == prefix and fragment in document
            }
            if not named:
                continue
            if len(named) == 1:
                document, (lowest, highest) = next(iter(named.items()))
                if first == lowest and last != highest:
                    stale.append(
                        f"{label} cites {prefix}-{first}…{prefix}-{last} beside {document}, "
                        f"which declares {prefix}-{lowest}…{prefix}-{highest}"
                    )
            break
    return stale


def test_a_range_cited_beside_its_own_document_ends_at_that_documents_last_id():
    """Item 21's other half: a range citation whose end has moved.

    **The citation has to name the document it indexes**, in the two hundred characters before
    it, and start at that document's first id. That is `docs/BUILD-HANDOFF.md`'s index row and
    it is where `ad67a14` wrote `R-8…R-28` in the commit that added R-29 and R-30.

    **What it lets through, and it is most range citations in this repository.** A range with no
    document named beside it is never checked — `epics-effects.md`'s *"the architecture spine's
    AD-16…AD-31"* names the spine in words, not as a path, and is invisible here. So is every
    interior range (`R-25 to R-28`, `FX-1 – FX-3`, `TP-11 – TP-13`), which is right: those go on
    meaning what they meant when a ruling is added. So is any range whose leading text matches
    two defining documents at once. And an id declared anywhere but a markdown heading does not
    exist as far as this scan is concerned.
    """
    stale: list[str] = []
    for path in documents():
        stale += stale_range_citations(where(path), path.read_text(encoding="utf-8"))
    assert not stale, "\n".join(stale)


#: The rulings file's own last id, derived rather than written down.
#:
#: **The negative controls below used to hard-code it, and that went stale the first time the file
#: grew** -- on 2026-08-28, when R-34..R-41 landed and three tests in this module failed for the
#: reason the module exists to catch. A guard whose fixture carries the very number the guard
#: watches is a guard that bills its own maintenance to whoever adds a ruling, which is how a check
#: earns a reputation for noise and then gets deleted. It is read from the file.
RULINGS_LAST = max(
    int(found)
    for found in re.findall(
        r"^## R-(\d+)",
        (REPO / "_bmad-output/planning-artifacts/effects-director-rulings-2026-08-24.md")
        .read_text(encoding="utf-8"),
        re.MULTILINE,
    )
)


#: **A control may not borrow a real identifier whose presence is what the scan watches.**
#:
#: This probe said *"There is no generator for `xfade` and no caller"* until 2026-08-28, chosen
#: because `xfade` appeared nowhere in the package -- which made it a perfect negative control right
#: up until Epic 11's first slice added `xfade` to `assembly.py`, at which point the claim became
#: genuinely false and the scan correctly fired on its own fixture. The test was red for a reason
#: that was not a defect, during someone else's work, on a module they did not own.
#:
#: It is the second instance of one mistake in this module: the range control hard-coded the
#: rulings file's last id and failed every time a ruling was added. Both billed the guard's
#: maintenance to whoever did the work the guard exists to protect, which is exactly how a check
#: earns a reputation for noise and then gets deleted.
#:
#: The rule that came out of it: **a control names something that can never exist** -- here a
#: deliberately impossible identifier -- so it tests the scan's mechanism without depending on the
#: state of the thing under observation. The positive controls above are safe as they are, because
#: they assert a claim *does* fire about a name that is present and will stay present.
@pytest.mark.parametrize(
    ("text", "fires"),
    [
        ("`effects-director-rulings-2026-08-24.md` (R-8…R-28)", True),
        (f"`effects-director-rulings-2026-08-24.md` (R-8…R-{RULINGS_LAST})", False),
        ("`effects-director-rulings-2026-08-24.md` (R-25 to R-28)", False),
        (f"`effects-director-rulings-2026-08-24.md` (~~R-8…R-28~~ R-8…R-{RULINGS_LAST})", False),
        ("the rulings, R-8…R-28", False),
    ],
)
def test_the_range_scan_fires_on_the_citation_ad67a14_wrote(text, fires):
    """The positive control for the range scan, on the citation `ad67a14` actually wrote.

    The last row is the honest cost of the design: the same short range with no document named
    beside it goes past, and this test says so rather than leaving it to be discovered.
    """
    assert bool(stale_range_citations("probe", text)) is fires


#: The deferred-work ledger, which is the one document in this repository whose **position** is a
#: claim. Everything under `## Resolved` asserts, by sitting there, that it shipped.
LEDGER = REPO / "_bmad-output" / "implementation-artifacts" / "deferred-work.md"

#: The heading whose position is the claim. **One constant, read by the scan and by the
#: self-check**, because they disagreed: the self-check was a *substring* test while the scan's
#: predicate is exact line equality, so renaming the heading to `## Resolved work` left the guard
#: asserting it was watching something and scanning nothing. Mutating the heading that way
#: survived all 2783 tests on 2026-08-30, and the failure message below promises the opposite --
#: *"this guard moves with it rather than being deleted"*.
LEDGER_RESOLVED_HEADING = "## Resolved"


def ledger_resolved_section(text: str) -> list[str] | None:
    """The lines the `## Resolved` heading owns, or `None` where there is no such heading.

    **Exact line equality**, which is the predicate the scan has always used and the one the
    self-check now uses too. And it **stops at the next `## ` heading**: a section added after
    this one is not under it, and reading to the end of the file would have made every later
    section's contents into claims this heading never made.
    """
    lines = text.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip() == LEDGER_RESOLVED_HEADING
        ),
        None,
    )
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return body


def misfiled_ledger_entries(ledger: Path | None = None) -> list[str]:
    """Every entry under `## Resolved` that carries no `resolved:` line, by its summary.

    An entry is a `- source_spec:` line and the indented lines under it. The `resolved:` line is
    what makes the heading's claim true for that entry; without one, the entry is open work filed
    under a heading that says it is finished.

    **One-directional, deliberately.** A shipped entry annotated in place *above* the heading is
    not scanned: it over-reports open work, which costs a reader a re-read and misleads nobody.
    The failure this exists for is the other one.

    Three corrections to the parser, 2026-08-31, each of them a way an entry could sit here and
    be invisible to the scan that is about it:

    * **an indented bullet is still a bullet.** `  - source_spec:` was read as a continuation
      line of the entry above it, so a nested or re-indented entry inherited its neighbour's
      `resolved:` line and went unreported.
    * **indentation may be a tab.** The continuation test was `startswith("  ")`, so a
      tab-indented `resolved:` line was not part of its own entry and the entry read as misfiled
      -- the guard's *other* failure direction, a false report, which is how a guard gets
      switched off.
    * **`resolved:` needs a value.** A bare `resolved:` with nothing after it is the shape a
      truncated append leaves, and it satisfied the claim by existing.
    """
    body = ledger_resolved_section((ledger or LEDGER).read_text(encoding="utf-8"))
    if body is None:  # pragma: no cover - the heading's presence is asserted by the test
        return []

    def says_where_it_shipped(item: str) -> bool:
        stripped = item.strip()
        return bool(
            stripped.startswith("resolved:") and stripped[len("resolved:") :].strip()
        )

    misfiled: list[str] = []
    entry: list[str] = []
    for line in [*body, "- source_spec: sentinel"]:
        if line.lstrip().startswith("- source_spec:"):
            if entry and not any(says_where_it_shipped(item) for item in entry):
                summary = next(
                    (item.strip()[9:] for item in entry if item.strip().startswith("summary:")),
                    entry[0].strip(),
                )
                misfiled.append(summary[:120])
            entry = [line]
        elif entry and line[:1].isspace():
            entry.append(line)
    return misfiled


def test_the_deferred_ledgers_resolved_section_holds_only_resolved_work():
    """Nothing under `## Resolved` without a `resolved:` line saying where it shipped.

    **This is a guard against a lie no reader of an entry can see.** On 2026-08-30 fourteen open
    gaps sat under that heading -- three from Epic 11, six from Epic 10, two from Epic 8 and three
    older -- and every one of them was *correct*: the right summary, the right evidence, the right
    source spec. What was false was where they sat, and they sat there because appending an entry
    to a markdown file means appending to the end of the file, while the end of the file has been
    inside the `## Resolved` section since that section was added. My own append script did it
    twice in one epic, holding the record open in front of me both times.

    So the scan is positional, because the defect is. It is the same kind as this module's other
    two -- a record the repository writes about itself and then falsifies -- with the difference
    that no amount of care while writing the entry could have prevented it.
    """
    assert LEDGER.exists(), f"{where(LEDGER)} is the ledger this scan is about"
    # **The self-check asks the scan's own question** (2026-08-31). It was
    # `"## Resolved" in LEDGER.read_text(...)` -- a substring, where the scan matches a whole
    # line -- so renaming the heading to `## Resolved work` satisfied the check and left the scan
    # reading nothing, silently, while this test went on passing. That mutation survived all 2783
    # tests, which is precisely what the sentence below promises it would not.
    assert ledger_resolved_section(LEDGER.read_text(encoding="utf-8")) is not None, (
        f"{where(LEDGER)} has no line reading exactly `{LEDGER_RESOLVED_HEADING}`, so this scan "
        "is watching nothing. If the ledger was restructured, this guard moves with it rather "
        "than being deleted."
    )
    misfiled = misfiled_ledger_entries()
    assert not misfiled, (
        f"{len(misfiled)} entries under `## Resolved` in {where(LEDGER)} carry no `resolved:` "
        "line, so that heading claims work shipped that has not. A new entry goes at the end of "
        "the **open** section, above the heading:"
        + "".join(f"{chr(10)}  - {summary}" for summary in misfiled)
    )


@pytest.mark.parametrize(
    ("entry", "misfiled"),
    [
        ("- source_spec: x", True),
        ("- source_spec: x\n  summary: s", True),
        ("- source_spec: x\n  summary: s\n  resolved: abc1234 -- shipped", False),
        # A tab is indentation. Before 2026-08-31 the continuation test was `startswith("  ")`,
        # so this entry's `resolved:` line belonged to no entry and the entry read as misfiled --
        # the guard's false-report direction, which is how a guard gets switched off.
        ("- source_spec: x\n\tsummary: s\n\tresolved: abc1234 -- shipped", False),
        # A `resolved:` line with nothing after it says where nothing shipped. It is the shape a
        # truncated append leaves, and it used to satisfy the claim by existing.
        ("- source_spec: x\n  summary: s\n  resolved:", True),
        ("- source_spec: x\n  summary: s\n  resolved:   ", True),
        # An indented bullet is still a bullet. Read as a continuation line it inherited the
        # entry above it -- so a re-indented entry with no `resolved:` line of its own was
        # invisible to the scan that exists for it.
        (
            (
                "- source_spec: x\n  summary: s\n  resolved: abc1234 -- shipped\n"
                "  - source_spec: y\n    summary: nested and open"
            ),
            True,
        ),
    ],
)
def test_the_ledger_scan_fires_on_an_entry_with_no_resolved_line(tmp_path, entry, misfiled):
    """The positive control, built on its own file rather than read off the ledger.

    That is this module's own rule, learned twice: a control that depends on the state of the
    thing under observation bills its own maintenance to whoever does the work the guard exists to
    protect. This one says the same thing on both sides of any future correction to the ledger.

    The rows are the shapes that occur and the ones that must not fire. A bare `- source_spec:`
    with nothing under it is included because that is what a truncated append leaves behind, and a
    scan that needed a `summary:` line to notice would go quiet on exactly the worst case. The
    last four rows are 2026-08-31's, one per way an entry could sit under that heading and be
    invisible to the scan.
    """
    probe = tmp_path / "deferred-work.md"
    probe.write_text(
        "# Deferred Work\n\n- source_spec: open\n  summary: still open\n\n"
        "## Resolved\n\nKept for the audit trail.\n\n" + entry + "\n",
        encoding="utf-8",
    )
    assert bool(misfiled_ledger_entries(probe)) is misfiled


def test_the_ledger_scan_reads_its_own_heading_and_stops_at_the_next_one(tmp_path):
    """The two ends of the section, which nothing held.

    **The heading**: the scan matches a whole line and the self-check matched a *substring*, so
    `## Resolved work` passed the check and scanned nothing -- a guard reporting that it is
    watching while it watches nothing, which is the exact failure mode this module exists for.

    **The end of the section**: everything to the end of the file used to be under this heading,
    so an entry filed under a later `## ` heading was reported as a lie told by *this* one.
    """
    def probe(text: str) -> Path:
        written = tmp_path / f"ledger-{abs(hash(text))}.md"
        written.write_text(text, encoding="utf-8")
        return written

    open_entry = "- source_spec: x\n  summary: still open\n"
    shipped = "- source_spec: y\n  summary: done\n  resolved: abc1234 -- shipped\n"

    # Renamed: there is no `## Resolved` line, so the scan is watching nothing -- and the
    # self-check above is what has to notice, which is why this asserts the section is `None`
    # rather than asserting the scan stays quiet.
    renamed = "# Deferred Work\n\n## Resolved work\n\n" + open_entry
    assert ledger_resolved_section(renamed) is None
    assert misfiled_ledger_entries(probe(renamed)) == []

    # And under the right heading the same entry is reported.
    exact = "# Deferred Work\n\n## Resolved\n\n" + open_entry
    assert ledger_resolved_section(exact) is not None
    assert misfiled_ledger_entries(probe(exact)) == ["still open"]

    # A later section is not this heading's claim: the open entry under `## Notes` is somebody
    # else's business, and the shipped one above it keeps the section honest.
    followed = (
        "# Deferred Work\n\n## Resolved\n\n" + shipped + "\n## Notes\n\n" + open_entry
    )
    assert misfiled_ledger_entries(probe(followed)) == []
