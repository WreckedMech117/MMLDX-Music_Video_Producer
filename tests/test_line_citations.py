"""A line number is a claim about a revision, so it is checked and it is counted.

Epic 9's retrospective item **40** named **citations** as its sixth instance type, and Epic 10's
retrospective proved it on itself. Its findings were anchored at `4fd9b41`; the fix pass that
followed added roughly three hundred lines to `app.py` and `effects.py`, and by `faafb55` an
implementer working from those citations had to re-find every one and said so. Two commits.
Epic 9's A7 is the older instance: R-20 cited `tests/test_effects.py:215` for a test that has
never been at that line — it was at 265, then 267, and is at 271 now.

**Two scans, because the two failures are different.** A citation can be *broken* — it names a
line that is not there — and a citation can be *unanchored*, which is not broken today and is
what goes stale. The first is checked against the file; the second is counted.

**The corpus is the one `test_stale_claims` already defines**, imported rather than re-listed so
that "the documents that describe this application as it stands" has one definition. That
excludes `_bmad-output/implementation-artifacts/` and `docs/DEVELOPMENT-LOG.md`, both dated
records, for the reason that module gives: a retrospective's `app.py:7690` was true on the day
it was written, and forcing it to be true now would convert a genuine record into a lie. **586
of this repository's 685 line citations** sit in those excluded records — 552 in the specs and
retrospectives, 34 in the development log — and none of them is this module's business.

**Only files this repository tracks are resolvable.** Measured over the corpus: of its **99**
line citations, **48 name files outside this repository** — `comfy/model_management.py`,
`comfy_extras/nodes_model_advanced.py`, ComfyUI's `nodes.py`, `sageattention/core.py`. They are
citations into a user-managed application at a version this repository does not pin, and
nothing here can or should check them. A bare filename resolves only when exactly one tracked
path ends with it, so the **6** citations to `timeline.py` and `models.py` — each of which two
tracked paths answer to — resolve to nothing either. That leaves **45**.

**Why the second scan is a census and not a prohibition.** Requiring every citation to carry a
revision or name a symbol was measured before it was rejected: of those 45, exactly **one**
carries a sha anywhere near it. Closing that means editing Director rulings, an architecture
spine and three dated investigation records to remove line numbers they were written with — and
a second measurement said it would buy little: of the 76 places in the whole repository where a
backticked snippet sits directly against a citation, only 5 still corroborate at HEAD, and every
one of the 71 misses is in a dated record that was right when it was written. So the census
freezes the existing debt instead of repaying it, and the rule it enforces is the forward one:
**a document may not gain a bare line citation.** Anchor it with a sha, or name the symbol, and
it does not count.

**What this lets through, stated plainly.**

- **Every citation already in the table below.** They are recorded, not repaired. A number here
  is debt this module has counted and chosen not to pay.
- **A citation that is in range and points at the wrong thing.** Nothing here reads what the
  cited line says. `tests/test_effects.py:215` — A7's instance — is in range and always was;
  this module would not have caught it, and the range scan in `test_stale_claims` would not
  either. Only a human comparing the sentence to the line catches that class.
- **Citations to files outside this repository**, and to bare filenames that name more than one
  tracked path. Both are counted in the census, so a new one is still visible, but neither is
  checked against anything.
- **The anchor is not verified.** A backticked seven-hex-digit word near a citation reads as a
  sha whether or not that commit exists, and nothing confirms the line was ever right at it.
- It reads markdown only. A line citation in a docstring, a comment or a commit message is
  outside every scan here.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache

import pytest
from test_stale_claims import REPO, documents, where

#: `app.py:7690`, `docs/ROADMAP.md:29`, `src/music_video_producer/effects.py:2470`. Backticks
#: are optional because this repository writes it both ways in the same paragraph.
CITATION = re.compile(
    r"\b([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|js|css|html|md|ya?ml|toml|json|cube))\s*:\s*(\d+)\b"
)

#: A revision, as this repository writes one: a short sha **in backticks**. Seven is `git`'s own
#: default abbreviation and the length every commit is quoted at in these documents. The
#: backticks are load-bearing rather than cosmetic — without them the `a64a0460` at the head of a
#: ComfyUI prompt uuid in `.memlog.md` reads as a commit and anchors the two citations beside it.
SHA = re.compile(r"`[0-9a-f]{7,40}`")

#: How far either side of a citation a sha still reads as its anchor. Wide enough for the shape
#: Epic 10's retrospective used — a sentence declaring the anchor, then the citations after it —
#: and narrow enough that a sha in the next finding does not cover this one.
ANCHORED_WITHIN = 240

#: Line citations that name a file this repository tracks, per document, measured 2026-08-28 at
#: `f22f66b` and after the one broken citation below it was repaired. This is a **ledger of
#: debt**, not a set of allowances: every number here is a place where a reader is being handed
#: a line number with no revision beside it. A document that gains one fails; a document that
#: pays one off fails too, and the fix is to lower the number in the same edit — the table is a
#: measurement, and a measurement that only ever ratchets one way stops being one.
#
# **19 -> 20 on 2026-08-28, and the cause is worth keeping.** Nothing in that document changed. The
# Director un-ignored `_bmad-output/implementation-artifacts/sprint-status.yaml`, and this census
# only counts a citation whose filename resolves against `git ls-files`. So
# `timeline-centric-layout-analysis.md:157`'s reference to `sprint-status.yaml:71-75` -- true then
# and true now, those lines really do show epic-6 and its three stories at `backlog` -- was
# invisible while the tracker was untracked and countable the moment it was not.
#
# The lesson for anyone editing this pin: **the census is a function of the tracked set, not only of
# the prose.** Adding or removing a tracked path can move it without a single document changing, and
# the honest response is to check the newly counted citation is *true* before re-pinning, which is
# what was done here. That is also this module's own instance of the rule it exists for -- a change
# to the repository made a record false in the same session.
UNANCHORED: dict[str, int] = {
    "_bmad-output/planning-artifacts/architecture/architecture-MusicVideoProducer-2026-08-16/.memlog.md": 1,
    "_bmad-output/planning-artifacts/architecture/architecture-MusicVideoProducer-effects-2026-08-21/ARCHITECTURE-SPINE.md": 1,
    "_bmad-output/planning-artifacts/effects-and-transitions-research-2026-08-21.md": 2,
    "_bmad-output/planning-artifacts/effects-director-rulings-2026-08-24.md": 8,
    "_bmad-output/planning-artifacts/h3-attention-backend-experiment.md": 9,
    "_bmad-output/planning-artifacts/prds/prd-MusicVideoProducer-treatment-2026-08-22/addendum.md": 1,
    "_bmad-output/planning-artifacts/timeline-centric-layout-analysis.md": 20,
    "_bmad-output/planning-artifacts/treatment-planning-findings-and-rulings-2026-08-22.md": 1,
    "docs/project-context.md": 2,
}


@lru_cache(maxsize=1)
def tracked() -> tuple[str, ...]:
    """Every path this repository tracks, as `git ls-files` reports them."""
    if not (REPO / ".git").exists():
        return ()
    result = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return ()
    return tuple(line for line in result.stdout.splitlines() if line.strip())


def resolve(name: str) -> str | None:
    """The tracked path a cited filename names, or `None` if it names none or several."""
    paths = tracked()
    if name in paths:
        return name
    candidates = [path for path in paths if path.endswith("/" + name)]
    return candidates[0] if len(candidates) == 1 else None


def citations(text: str) -> list[tuple[str, int, bool]]:
    """`(filename, line, anchored)` for every line citation in one document."""
    found: list[tuple[str, int, bool]] = []
    for match in CITATION.finditer(text):
        window = text[max(0, match.start() - ANCHORED_WITHIN) : match.end() + ANCHORED_WITHIN]
        found.append((match.group(1), int(match.group(2)), bool(SHA.search(window))))
    return found


def test_every_line_citation_names_a_line_that_is_there():
    """The broken half. A citation past the end of its file is wrong with no judgement needed.

    One existed when this was written: R-21 cited `app.py:13885` for a `Shot(...)` construction
    the router split had moved to `routes/unsorted.py`, in a file 663 lines shorter than the
    number it named. It now names the function.
    """
    broken: list[str] = []
    for path in documents():
        for name, line, _ in citations(path.read_text(encoding="utf-8")):
            target = resolve(name)
            if target is None:
                continue
            length = len((REPO / target).read_text(encoding="utf-8", errors="replace").splitlines())
            if line > length:
                broken.append(f"{where(path)} cites {name}:{line}; {target} has {length} lines")
    assert not broken, "\n".join(broken)


def test_no_document_has_gained_a_bare_line_citation():
    """The unanchored half, as a census. The table above says what each number is."""
    if not tracked():
        pytest.skip("not a git checkout, so no cited filename can be resolved")
    counted: dict[str, int] = {}
    for path in documents():
        total = sum(
            1
            for name, _, anchored in citations(path.read_text(encoding="utf-8"))
            if not anchored and resolve(name) is not None
        )
        if total:
            counted[where(path)] = total
    assert counted == UNANCHORED


def test_the_citation_scan_tells_broken_from_stale_from_anchored(tmp_path):
    """The positive control, on the three outcomes, so a broken regex cannot read as clean.

    The middle case is the one worth stating: a citation with a sha beside it is *not* counted,
    which is the whole escape hatch. Anchor a line number and this module stops having an
    opinion about it — including about whether the anchor is true.
    """
    assert citations("see `app.py:13885` for the shape") == [("app.py", 13885, False)]
    assert citations("at `4fd9b41`, `app.py:7690` is the census") == [("app.py", 7690, True)]
    assert citations("`comfy/sd.py:1104` is ComfyUI's") == [("comfy/sd.py", 1104, False)]

    assert resolve("effects.py") == "src/music_video_producer/effects.py"
    #: Two tracked paths end with it, so neither is what the citation names.
    assert resolve("timeline.py") is None
    #: ComfyUI's, and this repository does not have one.
    assert resolve("model_management.py") is None
