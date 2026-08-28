"""Spec frontmatter, checked against the two records that know whether the work shipped.

Epic 9's retrospective item **40** — *nothing checks a record against the thing it describes* —
named six instance types, and **spec frontmatter** is one of them. It was stale while the item
sat open: on 2026-08-28, ten specs under `_bmad-output/implementation-artifacts/` read
`status: 'approved'`, including every Epic 10 slice spec, all of them shipped and committed.
The vocabulary had drifted to five spellings across the corpus — `approved` (10), `done` (36),
`draft` (24), `implemented` (1), `in-progress` (2). A field with five spellings, one of them
used once, is a habit rather than a status.

**The vocabulary is read from the template that writes these files, not chosen here.**
`.claude/skills/bmad-build/spec-template.md` ships
`status: 'draft' # draft | ready-for-dev | in-progress | in-review | done`, and that comment is
the only declaration of the field's domain anywhere in this repository. Hard-coding a set in
this module would have made it one more prose statement of a rule; reading the template makes a
change to the tool a change to the gate. `approved` and `implemented` are outside that set,
which is how a status nobody could define survived two epics.

**What "shipped" means here, and it is two independent signals rather than one.** Neither can
see everything, so both are asserted and each is named:

- **The spec's own task list.** A spec with at least one `- [x]` and no `- [ ]` says, in its own
  body, that its work list is finished. `spec-song-context-on-import.md` sat at `in-progress`
  with seven of seven ticked.
- **The tracker.** A spec whose filename names a story (`spec-9-5-…` → `9.5`) or whose
  frontmatter names an epic (`epic: 10`) is shipped when `sprint-status.yaml` says that story or
  epic is `done`. That is what caught the ten `approved` specs.

**What this cannot see, stated rather than implied.**

- **A spec that names neither a story nor an epic and carries no task list is invisible.**
  `spec-router-split.md` is exactly that: seventy-six routes left `create_app` in `4a1a4f6` and
  `e6f6b23`, the work unquestionably shipped, and nothing in this module could have told. It was
  found by the vocabulary test instead, because `approved` is not a status — which is luck, not
  coverage.
- **It reads `done` as the end state and never checks the other direction.** A spec marked
  `done` whose work was abandoned passes here, and so does a `done` spec with unticked tasks —
  eight of them exist today, because several of this repository's specs record a task list the
  implementer diverged from deliberately.
- **`type` has drifted the same way and is not guarded.** The template declares
  `feature | bugfix | refactor | chore`; the corpus also uses `story`, `slice`, `fix` and
  `remediation`. Closing that means renaming seventy-four files' frontmatter to a vocabulary
  with no word for *slice*, which is a decision for the Director and not a defect this module
  should force.
- It reads the frontmatter block only — the first `---`-delimited section. A `status:` line in
  the body is not a status and is ignored.
- **On a fresh clone it checks nothing and says so.** The specs and the tracker are gitignored
  (`.gitignore:25`), so this module skips wherever they are absent rather than collecting zero
  cases and reading green. That is a real hole in a CI checkout and it is stated, not hidden.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPECS = REPO / "_bmad-output" / "implementation-artifacts"
TEMPLATE = REPO / ".claude" / "skills" / "bmad-build" / "spec-template.md"
TRACKER = SPECS / "sprint-status.yaml"

#: The one status that means the work is behind us. Everything else in the vocabulary is a
#: claim that it is still ahead, which is the claim a shipped spec must not be making.
SETTLED = "done"

#: The specs and the tracker are **not in version control**: `.gitignore:25` keeps everything
#: under `_bmad-output/implementation-artifacts/` out except `deferred-work.md`, on the reasoning
#: that a per-story spec is spent once its story lands. So this module reads a working tree's own
#: records, and on a fresh clone there are none. It says so and skips rather than collecting zero
#: cases and reading as a clean corpus, which is the failure mode of a scan over a missing corpus.
pytestmark = pytest.mark.skipif(
    not (TEMPLATE.exists() and TRACKER.exists() and any(SPECS.glob("spec-*.md"))),
    reason="the spec corpus and the tracker are gitignored; this checkout has none of them",
)


def declared_statuses() -> tuple[str, ...]:
    """The status vocabulary, read off the template's own frontmatter comment."""
    line = re.search(r"^status:.*#(.*)$", TEMPLATE.read_text(encoding="utf-8"), re.MULTILINE)
    assert line, f"{TEMPLATE.name} no longer declares the status vocabulary in a comment"
    return tuple(word.strip() for word in line.group(1).split("|"))


def frontmatter(path: Path) -> dict[str, str]:
    """The `key: value` pairs of a markdown file's leading `---` block, values unquoted."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    block = text.split("---", 2)
    if len(block) < 3:
        return {}
    found: dict[str, str] = {}
    for line in block[1].splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$", line)
        if match:
            found[match.group(1)] = match.group(2).strip().strip("'\"")
    return found


def specs() -> list[Path]:
    """Every spec, discovered rather than listed."""
    return sorted(SPECS.glob("spec-*.md"))


def tracker_statuses() -> dict[str, str]:
    """`development_status`'s flat `key: value` map, read without a YAML parser.

    PyYAML is in this environment only transitively, through `faster-whisper`; importing it
    here would put a gate on a package nothing declares. The block is machine-generated, two
    spaces deep, one scalar per line, so reading it directly costs less than the dependency
    question would.
    """
    found: dict[str, str] = {}
    inside = False
    for line in TRACKER.read_text(encoding="utf-8").splitlines():
        if re.match(r"^development_status:\s*$", line):
            inside = True
            continue
        if inside and line and not line.startswith(" "):
            break
        match = re.match(r"^  ([A-Za-z0-9][A-Za-z0-9-]*):\s*(\S+)\s*$", line)
        if inside and match:
            found[match.group(1)] = match.group(2)
    assert found, "development_status parsed empty, so every assertion built on it is vacuous"
    return found


def task_list(path: Path) -> tuple[int, int]:
    """`(ticked, open)` checkbox counts in a spec's body."""
    text = path.read_text(encoding="utf-8")
    return (
        len(re.findall(r"^\s*-\s*\[x\]", text, re.MULTILINE | re.IGNORECASE)),
        len(re.findall(r"^\s*-\s*\[ \]", text, re.MULTILINE)),
    )


def shipping_evidence(path: Path) -> list[str]:
    """Why this spec's work is behind us, in the words of the records that say so."""
    reasons: list[str] = []
    ticked, still_open = task_list(path)
    if ticked and not still_open:
        reasons.append(f"its own task list is {ticked} of {ticked} ticked")

    tracker = tracker_statuses()
    story = re.match(r"^spec-(\d+)-(\d+)-", path.name)
    if story:
        prefix = f"{story.group(1)}-{story.group(2)}-"
        settled = [key for key in tracker if key.startswith(prefix) and tracker[key] == SETTLED]
        if settled:
            reasons.append(f"the tracker has {settled[0]}: {SETTLED}")

    epic = frontmatter(path).get("epic")
    if epic and tracker.get(f"epic-{epic}") == SETTLED:
        reasons.append(f"the tracker has epic-{epic}: {SETTLED}")
    return reasons


def test_the_status_vocabulary_is_the_one_the_template_declares():
    """The read is pinned, so a widened template is a deliberate change and not a silent one."""
    assert declared_statuses() == ("draft", "ready-for-dev", "in-progress", "in-review", "done")


@pytest.mark.parametrize("spec", [path.name for path in specs()])
def test_the_spec_carries_a_status_from_the_declared_vocabulary(spec):
    """One case per spec, so a green run names which of them were looked at."""
    status = frontmatter(SPECS / spec).get("status")
    assert status, f"{spec} carries no status in its frontmatter"
    assert status in declared_statuses(), (
        f"{spec} is `{status}`, which {TEMPLATE.name} does not declare; the vocabulary is "
        f"{' | '.join(declared_statuses())}"
    )


def test_no_spec_whose_work_has_shipped_still_says_it_is_waiting():
    """Item 40's second instance type: the record, against the two things that describe it."""
    stale: list[str] = []
    for path in specs():
        status = frontmatter(path).get("status", "")
        if status == SETTLED:
            continue
        reasons = shipping_evidence(path)
        if reasons:
            stale.append(f"{path.name} is `{status}` but {'; and '.join(reasons)}")
    assert not stale, "\n".join(stale)


def test_the_shipped_scan_reads_both_signals_and_says_so_when_it_reads_neither(tmp_path):
    """The positive control. Without it a broken parse reads as a clean corpus.

    Three probes, one per outcome: a spec whose own task list is complete, a spec that names an
    epic the tracker calls done, and a spec that offers neither — which is the hole named in
    this module's docstring, asserted here rather than only described.
    """
    finished = tmp_path / "spec-probe-finished.md"
    finished.write_text(
        "---\nstatus: 'in-progress'\n---\n- [x] one\n- [x] two\n", encoding="utf-8"
    )
    assert shipping_evidence(finished) == ["its own task list is 2 of 2 ticked"]

    by_epic = tmp_path / "spec-probe-epic.md"
    by_epic.write_text("---\nstatus: 'draft'\nepic: 10\n---\nno task list\n", encoding="utf-8")
    assert shipping_evidence(by_epic) == ["the tracker has epic-10: done"]

    silent = tmp_path / "spec-probe-silent.md"
    silent.write_text("---\nstatus: 'draft'\n---\n- [ ] not started\n", encoding="utf-8")
    assert shipping_evidence(silent) == []
