"""*Everywhere*, *both* and *all three* are claims about work, and a claim must name its instances.

Epic 9's retrospective item **42**: *an action item phrased "everywhere" gets executed at the
instances its author has in hand; it should carry the enumeration or say where it stopped.* Both
of Epic 8's incomplete items were that shape — item 18 said the envelope size was corrected
"everywhere" and `docs/BUILD-HANDOFF.md` still read `~750 KB`, in the file a new session opens
first. Epic 10 then recurred it **inside a ruling written during the epic**: R-29 states *"Story
10.1's AC and the epic's headline are amended a second time."* The headline was amended. The AC
was not, and went on saying *"Geometry, Texture, Grade and Stylize are all bindable"* while every
declared number in Geometry was refused. The retrospective found it two days later.

**This is the guard most likely to become noise, and the scope is where the noise is kept out.**
*Everywhere*, *every*, *both* and *all three* are ordinary English; almost every use of them in
this repository is a description of the world rather than a report of work performed. So this
module reads **two corpora and no others**: the Director rulings under
`_bmad-output/planning-artifacts/`, and the `action_items` in `sprint-status.yaml`. Those are the
two places where someone writes down that a set of records has been changed, or commissions it,
and they are the two places item 42's instances have actually occurred.

**The two corpora get different triggers, because a ruling reports and an item commissions.**

- **A ruling** fires only on a sentence that both *reports an amendment in the performed voice*
  (`amended`, `corrected`, `struck`, `reconciled`, `re-verified`, or the noun `amendment`) and
  *states an arity* (`both`, `all three`, `two amendments`, `everywhere`). Measured over the
  three rulings documents: seven sentences carry an arity and an action word of any kind; the
  narrow amendment vocabulary left **three** of those, one of them R-29's — and two now that
  R-29's has been struck and corrected. *"Both compose as a branch"* and *"Both the count and
  the rate are recorded fields"* are descriptions and are outside it by construction, not by
  exemption.
- **An action item** fires on the arity alone, because an item is a task and its verb is an
  instruction. **Bare `both` is deliberately not a trigger here**: it occurs twice in
  sixty-five items — *"onsets outnumber beats on both real songs"* and *"or strike the comment.
  Not both."* — and neither is a scope claim. A quantifier inside quotes is not a trigger
  either, which is what keeps item 42, the item that quotes the word, from firing on itself.

**What counts as carrying the enumeration.** As many *locatable* targets, in the same block, as
the arity states — and a target is a **record**, not any word in backticks: a filename with or
without a line, a planning id (`R-29`, `AD-28`, `FX-12`), a `Story 10.1`, an `AC6`, a bare
capitalised document name like `ROADMAP`, or a `snake_case` name so that two docstrings in one
file can be enumerated at all. `crop`, `blend` and `strength` are filter options that happen to
be backticked, and counting them would have let R-29 through. An unbounded
quantifier — *everywhere*, *every instance* — needs at least two, because one instance is not an
enumeration. **Or the block says where it stopped**, in those words: item 42's own remedy is
*"carry the enumeration or say where it stopped"*, so a block containing *not enumerated*,
*never enumerated*, *not recorded* or *where it stopped* satisfies it. That escape is honest
only because it is loud; a block that uses it is announcing that the set is unknown.

**What this lets through, and the first one is the whole point.**

- **It checks that the enumeration is *present*, never that it is *complete*.** R-29 fires
  because it claims two and names one — *"Story 10.1's AC"*, with nothing for the headline. Had
  it said *"`epics-effects.md`'s Story 10.1 AC and its headline are amended"* it would pass, and
  the AC would still have been unamended. **Nothing mechanical closes that**; only a reader
  opening `epics-effects.md` closes it. What this buys is that the reader is told where to look,
  which is exactly what R-29 withheld.
- **Every ruling and item that states no arity.** *"Correct the spine's filename convention row"*
  is a single-instance claim and is never read here, even when the row turns out to exist in four
  documents. The failure this module names begins with the author counting.
- **Prose enumerations.** *"…lived only in three places: the story specs, code comments, and
  commit messages"* names three things and backticks one; if a sentence like that ever carries
  an amendment verb it will fire, and the fix is to backtick what it names.
- **Retrospectives, specs, the architecture spine and every other document.** An "everywhere"
  written in a retrospective's findings is outside this scan. That is where item 42's *evidence*
  usually lives, but not where its instances are committed.
- **Struck text.** `~~…~~` is exempt, the same exemption `test_stale_claims` makes: it is this
  repository's mark for a claim already recorded as wrong, and R-29's own correction uses it.
- **On a fresh clone the action-item half checks nothing and says so.** `sprint-status.yaml` is
  gitignored (`.gitignore:25`), so the module skips where it is absent rather than reading green
  over the rulings alone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLANNING = REPO / "_bmad-output" / "planning-artifacts"
TRACKER = REPO / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"

#: The rulings are tracked; `sprint-status.yaml` is not (`.gitignore:25`). Half this module has
#: nothing to read on a fresh clone, so it skips and says which half rather than passing.
pytestmark = pytest.mark.skipif(
    not TRACKER.exists(), reason="sprint-status.yaml is gitignored and this checkout has none"
)

WORD_COUNTS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

#: The nouns a count has to be counting for this to be a claim about records rather than about
#: the world. *"three places"* is in scope; *"three seconds"* and *"three bands"* are not.
RECORD_NOUN = (
    r"places|documents|files|instances|occurrences|locations|sites|claims|statements|"
    r"criteria|amendments|corrections|rows|sentences|comments|docstrings|call sites"
)

#: An unbounded quantifier: the author is asserting a set they have not counted.
UNBOUNDED = re.compile(
    r"\beverywhere\b|\bevery (?:instance|occurrence|place|one|other|case|site|caller|"
    r"document|file|row|claim)\b",
    re.IGNORECASE,
)

#: A count *over a record noun* — the only bounded shape an action item is judged on, because an
#: item's bare `both` is ordinary English twice out of twice in this tracker.
COUNTED_RECORDS = re.compile(
    rf"\b(?:in )?({'|'.join(WORD_COUNTS)})\s+(?:{RECORD_NOUN})\b", re.IGNORECASE
)

#: A bounded one, and the number it states.
BOUNDED = re.compile(
    rf"\ball (?:{'|'.join(WORD_COUNTS)})\b|\bboth\b|"
    rf"\b(?:in )?(?:{'|'.join(WORD_COUNTS)}) (?:{RECORD_NOUN})\b",
    re.IGNORECASE,
)

#: A ruling reporting that a record has been changed. Narrow on purpose: `made`, `added` and
#: `recorded` were all measured against this corpus and each one pulled in a description of
#: behaviour rather than a report of an edit.
AMENDED = re.compile(
    r"\bamended\b|\bamendment(?:s)?\b|\bcorrected\b|\bcorrection(?:s)?\b|\bstruck\b|"
    r"\breconciled\b|\bre-verified\b|\bre-dated\b",
    re.IGNORECASE,
)

#: Item 42's own alternative, in its own words. A block saying this is not hiding anything.
STOPPED = re.compile(
    r"not enumerated|never enumerated|not recorded|nowhere recorded|where it stopped",
    re.IGNORECASE,
)

#: Something a reader can go and open: a **record**, not any identifier that happens to be
#: backticked. A filename with or without a line, a planning id, a story, or one of the bare
#: capitalised document names this repository writes without backticks (`ROADMAP`, `SPINE`,
#: `BUILD-ORDER`). The distinction is what R-29 turns on — its sentence backticks `crop`, which
#: is a filter option and not a place anything was amended.
TARGET = re.compile(
    r"\b[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|js|css|html|md|ya?ml|toml|json)(?::\d+)?"
    r"|\b[A-Z][A-Z-]{1,7}-\d+\b"
    r"|\bStory \d+\.\d+\b"
    r"|\bAC\d+\b"
    r"|\b[A-Z]{4,}(?:-[A-Z]+)*\b"
    #: A snake_case name, so two docstrings in one file can be enumerated at all. The underscore
    #: is what keeps `crop`, `blend` and `strength` — ordinary words that happen to be backticked
    #: filter options — from reading as places a record was amended.
    r"|\b_?[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b"
)

#: A quantifier the author is quoting rather than using. Item 42 is the whole reason this exists.
QUOTED = re.compile(r"['\"`‘’“”][^'\"`‘’“”]{0,40}"
                    r"(?:everywhere|every instance|both|all three)")


def arity(text: str) -> int | None:
    """How many instances `text` claims, or `None` if it claims no particular number."""
    if UNBOUNDED.search(text):
        #: One place is not an enumeration, so an uncounted claim still has to name two.
        return 2
    match = BOUNDED.search(text)
    if not match:
        return None
    word = re.search(rf"\b({'|'.join(WORD_COUNTS)})\b", match.group(0), re.IGNORECASE)
    return WORD_COUNTS[word.group(1).lower()] if word else 2


def targets(text: str) -> list[str]:
    """The distinct locatable things `text` names."""
    return sorted({found.group(0) for found in TARGET.finditer(text)})


def blocks(text: str) -> list[str]:
    """Paragraphs, flattened, with headings dropped.

    A heading is a label for the section under it, not the report itself: F-5 in the treatment
    rulings is titled *"The character path's two known issues — both stale, re-verified"* and
    enumerates both in the bullets below, which is a different paragraph. Reading a heading as a
    claim in its own right made that the only false positive in the corpus; widening the block to
    swallow the whole section instead would have exempted R-29, whose section mentions R-25 in
    passing and would have counted it as a target. So headings are dropped and the body is read.
    """
    body = re.sub(r"^#{1,6} .*$", "", text, flags=re.MULTILINE)
    #: `~~…~~` removed, the same exemption `test_stale_claims` makes and for the same reason:
    #: it is this repository's mark for a claim it has already recorded as false, and keeping
    #: the wrong sentence visible beside its correction is the idiom here.
    body = re.sub(r"~~.*?~~", " ", body, flags=re.DOTALL)
    return [" ".join(part.split()) for part in re.split(r"\n\s*\n", body) if part.strip()]


def sentences(block: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", block)


def rulings() -> list[Path]:
    """The Director's rulings, discovered rather than listed."""
    return sorted(path for path in PLANNING.glob("*.md") if "ruling" in path.name)


def action_items() -> list[tuple[str, str]]:
    """`(id, action)` for every item in the tracker, read without a YAML parser.

    An `action` is a double-quoted scalar folded over as many lines as it needs; the folding is
    plain whitespace, so joining the continuation lines with a space restores it exactly.
    """
    found: list[tuple[str, str]] = []
    identifier = ""
    collecting: list[str] | None = None
    for line in TRACKER.read_text(encoding="utf-8").splitlines():
        if collecting is not None:
            collecting.append(line.strip())
            if line.rstrip().endswith('"'):
                found.append((identifier, " ".join(collecting).strip().strip('"')))
                collecting = None
            continue
        start = re.match(r'^\s*-?\s*id:\s*"(.*)"\s*$', line)
        if start:
            identifier = start.group(1)
            continue
        action = re.match(r'^\s*action:\s*"(.*)$', line)
        if action:
            body = action.group(1)
            if body.endswith('"'):
                found.append((identifier, body[:-1]))
            else:
                collecting = [body]
    assert found, "no action items parsed, so every assertion built on them is vacuous"
    return found


def unenumerated_rulings() -> list[str]:
    """Every ruling sentence reporting an amendment across a stated arity and naming too few."""
    short: list[str] = []
    for path in rulings():
        for block in blocks(path.read_text(encoding="utf-8")):
            if STOPPED.search(block):
                continue
            named = len(targets(block))
            for sentence in sentences(block):
                if not AMENDED.search(sentence):
                    continue
                wanted = arity(sentence)
                if wanted is not None and named < wanted:
                    short.append(
                        f"{path.name} claims {wanted} and its block names {named}: {sentence}"
                    )
    return short


def unenumerated_items() -> list[str]:
    """Every action item claiming a scope it does not name the instances of."""
    short: list[str] = []
    for identifier, action in action_items():
        if STOPPED.search(action):
            continue
        scrubbed = QUOTED.sub(" ", action)
        if UNBOUNDED.search(scrubbed):
            wanted = 2
        else:
            #: Bare `both` and bare `all three` are ordinary English in an item; only a count
            #: over a record noun, or an unbounded quantifier, is a scope claim here.
            counted = COUNTED_RECORDS.search(scrubbed)
            if not counted:
                continue
            wanted = WORD_COUNTS[counted.group(1).lower()]
        named = len(targets(action))
        if named < wanted:
            short.append(f"{identifier} claims {wanted} and names {named}: {action}")
    return short


def test_no_ruling_reports_an_amendment_it_does_not_name_the_targets_of():
    """R-29's shape: *"Story 10.1's AC and the epic's headline are amended"*, naming neither."""
    short = unenumerated_rulings()
    assert not short, "\n".join(short)


def test_no_action_item_claims_a_scope_it_does_not_enumerate():
    """Epic 8's item 18: *"correct the envelope size everywhere"*, naming one of four places."""
    short = unenumerated_items()
    assert not short, "\n".join(short)


def test_the_scan_fires_on_r29_and_lets_the_ruling_beside_it_stand():
    """The positive control, on the two sentences the rulings document actually holds.

    R-31 is the contrast that makes the rule fair rather than arbitrary: it makes the same kind
    of claim, in the same document, on the same day, and it names what it changed.
    """
    r29 = (
        "A Geometry parameter's bind glyph stays dim and refuses by name, saying that ffmpeg "
        "aborts when both `crop` dimensions move. Story 10.1's AC and the epic's headline are "
        "amended a second time — the headline has now lost *both* its examples."
    )
    r31 = (
        "Two amendments follow and are made: the AC and UX-DR7 in `epics-effects.md`, and "
        "`DESIGN.md` §6 and `EXPERIENCE.md`'s readout section."
    )
    assert arity(r29) == 2 and len(targets(r29)) < 2
    assert arity(r31) == 2 and len(targets(r31)) >= 2

    #: And the two sentences in this corpus that use *both* about the world rather than about
    #: work carry no amendment word, which is what keeps them out.
    assert not AMENDED.search("Both compose as a branch, so `strength` is written into `blend`.")
    assert not AMENDED.search("Both the count and the rate are recorded fields on every envelope.")


def test_the_item_scan_ignores_a_quoted_quantifier_and_a_bare_both():
    """The two noise sources measured in the sixty-five items, pinned so they stay excluded."""
    quoting = "PROCESS: an action item phrased 'everywhere' gets executed at the instances its "
    assert QUOTED.sub(" ", quoting).find("everywhere") == -1
    assert arity("onsets outnumber beats on both real songs") == 2
    assert not COUNTED_RECORDS.search("onsets outnumber beats on both real songs")
    assert not COUNTED_RECORDS.search("or strike the comment. Not both.")
    assert COUNTED_RECORDS.search("correct the two docstrings that assert the cap").group(1) == "two"
