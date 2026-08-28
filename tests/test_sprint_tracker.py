"""The tracker, against the commits attributed to it and against itself.

Epic 9's retrospective item **40** named **the tracker** as its first instance type, and the
instance was not subtle. `sprint-status.yaml` at the start of that retrospective said Epic 9 was
`backlog` with all six of its story keys `backlog` and no key for 9.7 at all, while thirteen
commits had shipped. Epic 8 said `in-progress` with its three stories at `review` — beside
`epic-8-retrospective: done`. **Epic 8's retrospective had already run against a file that did
not describe it**, and Epic 9's would have too. Both were repaired by hand, from commit
evidence, because the bundled `update` command has no story-status transition.

**Attribution is `git_evidence.py`'s, not a second answer to the same question.** `_parse_pass`
attributes a commit to a story by `re.search(rf"\\b{sid}\\b", subject)` against the subject line
alone, and `AGENTS.md` requires the story id in the subject in parentheses at the end. The story
ids come from the tracker's own keys — `10-1-bind-a-parameter-to-a-band` is story `10.1` — so a
key this file does not hold is a story this scan cannot see, which is right: the tracker is the
thing being checked.

**The self-checks need no git at all**, and each is a state this file has actually been in:

- An epic whose stories are all `done` that is not itself `done` — Epic 9's shape.
- An epic marked `done` holding a story that is not — the converse, never yet seen here.
- An epic whose retrospective is `done` while the epic is not — **Epic 8's exact shape**, and
  the one that let a retrospective run against a file describing a different epic.
- A story key naming an epic the file does not declare — how 9.7 would have been noticed.

**What this cannot catch, and the largest hole is the obvious one.**

- **A story that is `done` and should not be.** Nothing here reads code. A key can be moved to
  `done` with no commit, no spec and no feature, and every test in this module stays green. That
  is the direction Epic 9's repair went and it was verified by a human reading the diff.
- **A story whose commits do not name it.** Only twelve commits in this repository's history
  carry a story id: 9.5 and Epic 10's. Every commit before `AGENTS.md` was corrected is
  invisible here, so epics 1–9 are checked by their internal consistency and by nothing else.
  Attribution is the measurement that the rule works going forward, not a census of the past.
- **A subject that names a number it is not about.** `\\b10.1\\b` matches a version, a
  percentage or a timestamp as happily as a story id. The idiom this repository settled on —
  the id in parentheses at the end — is a convention the regex does not require.
- **`review`, `ready-for-dev` and `in-progress` are not judged against commits.** Only
  `backlog` is, because only `backlog` asserts that nothing has happened. Epics 1–3 sit at
  `review` with their work long shipped, which is a real staleness this module deliberately
  does not have the evidence to call.
- **On a fresh clone it checks nothing and says so.** `sprint-status.yaml` is gitignored
  (`.gitignore:25`), so this module skips where it is absent rather than reading green.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TRACKER = REPO / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"

#: The statuses that assert nothing has been done yet. A commit naming the story contradicts
#: them. `ready-for-dev` is here with `backlog`: the tracker's own legend says it means the
#: story file exists and development has not started.
NOT_STARTED = frozenset({"backlog", "ready-for-dev"})

#: `sprint-status.yaml` is **not in version control** — `.gitignore:25` keeps everything under
#: `_bmad-output/implementation-artifacts/` out but `deferred-work.md`. A checkout without it has
#: nothing to check, and saying so is better than a module that quietly asserts nothing.
pytestmark = pytest.mark.skipif(
    not TRACKER.exists(), reason="sprint-status.yaml is gitignored and this checkout has none"
)

EPIC_KEY = re.compile(r"^epic-(\d+)$")
RETRO_KEY = re.compile(r"^epic-(\d+)-retrospective$")
STORY_KEY = re.compile(r"^(\d+)-(\d+)-")


def development_status() -> dict[str, str]:
    """`development_status`'s flat `key: value` map, read without a YAML parser.

    PyYAML reaches this environment only transitively through `faster-whisper`; a gate that
    imported it would rest on a package nothing declares. The block is machine-generated, two
    spaces deep, one scalar per line.
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


def story_id(key: str) -> str | None:
    """`10-1-bind-a-parameter-to-a-band` -> `10.1`; anything else -> `None`."""
    match = STORY_KEY.match(key)
    return f"{match.group(1)}.{match.group(2)}" if match else None


@lru_cache(maxsize=1)
def subjects() -> tuple[str, ...] | None:
    """Every commit subject in this repository's history, or `None` outside a git checkout."""
    if not (REPO / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(REPO), "log", "--format=%s"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    return tuple(line for line in result.stdout.splitlines() if line.strip())


def attributed(identifier: str, lines: tuple[str, ...]) -> list[str]:
    """The subjects naming `identifier`, by `git_evidence.py`'s own word-boundary rule."""
    pattern = re.compile(rf"\b{re.escape(identifier)}\b")
    return [line for line in lines if pattern.search(line)]


def test_every_story_key_names_an_epic_the_file_declares():
    """A story with no epic is how 9.7's absence would have shown, from the other side."""
    status = development_status()
    orphans = [
        key
        for key in status
        if STORY_KEY.match(key) and f"epic-{STORY_KEY.match(key).group(1)}" not in status
    ]
    assert not orphans, f"story keys under no declared epic: {', '.join(orphans)}"


def test_an_epic_whose_stories_are_all_done_is_done_itself():
    """Epic 9's shape: six stories complete, the epic still `backlog`, for two retrospectives."""
    status = development_status()
    wrong: list[str] = []
    for key, value in status.items():
        epic = EPIC_KEY.match(key)
        if not epic:
            continue
        stories = {
            child: state
            for child, state in status.items()
            if STORY_KEY.match(child) and STORY_KEY.match(child).group(1) == epic.group(1)
        }
        if stories and all(state == "done" for state in stories.values()) and value != "done":
            wrong.append(f"{key} is `{value}` while all {len(stories)} of its stories are done")
    assert not wrong, "\n".join(wrong)


def test_an_epic_marked_done_holds_no_unfinished_story():
    """The converse, so `done` cannot be written over a story still in flight."""
    status = development_status()
    wrong: list[str] = []
    for key, value in status.items():
        epic = EPIC_KEY.match(key)
        if not epic or value != "done":
            continue
        for child, state in status.items():
            child_key = STORY_KEY.match(child)
            if child_key and child_key.group(1) == epic.group(1) and state != "done":
                wrong.append(f"{key} is done while {child} is `{state}`")
    assert not wrong, "\n".join(wrong)


def test_no_epic_has_a_finished_retrospective_and_an_unfinished_self():
    """Epic 8's exact shape, and the one that cost the most.

    `epic-8-retrospective: done` sat beside `epic-8: in-progress` with its three stories at
    `review`, which means a retrospective had already been run against a file that did not
    describe the epic it was reporting on. `optional` is not judged — it is the legend's word
    for a retrospective nobody has committed to.
    """
    status = development_status()
    wrong: list[str] = []
    for key, value in status.items():
        retro = RETRO_KEY.match(key)
        if retro and value == "done":
            epic = f"epic-{retro.group(1)}"
            if status.get(epic) != "done":
                wrong.append(f"{key} is done while {epic} is `{status.get(epic)}`")
    assert not wrong, "\n".join(wrong)


def test_no_story_with_commits_attributed_to_it_is_still_waiting_to_start():
    """The tracker against the commits, by `git_evidence.py`'s rule.

    Skipped outside a git checkout rather than passing vacuously: a green run must mean the
    history was read.
    """
    lines = subjects()
    if lines is None:
        pytest.skip("not a git checkout, so no commit can be attributed to anything")
    wrong: list[str] = []
    for key, value in development_status().items():
        identifier = story_id(key)
        if identifier and value in NOT_STARTED:
            commits = attributed(identifier, lines)
            if commits:
                wrong.append(
                    f"{key} is `{value}` and {len(commits)} commit(s) name {identifier}: "
                    f"{commits[0]!r}"
                )
    assert not wrong, "\n".join(wrong)


def test_the_attribution_rule_is_the_one_git_evidence_applies():
    """The positive control, on the two subjects this repository actually shipped.

    The word boundary is the whole rule: `9.5` must not be found in `19.55`, and the id in
    parentheses at the end — `AGENTS.md`'s convention, adopted after a `Story:` trailer was
    prescribed and measured to attribute nothing — must be found.
    """
    real = ("A look reaches a whole Section, and the rule that says which one is spelled once (9.5)",)
    assert attributed("9.5", real) == list(real)
    assert attributed("9.55", real) == []
    assert attributed("10.1", ("a subject mentioning 110.15 and nothing else",)) == []
    assert story_id("10-1-bind-a-parameter-to-a-band") == "10.1"
    assert story_id("epic-10-retrospective") is None
