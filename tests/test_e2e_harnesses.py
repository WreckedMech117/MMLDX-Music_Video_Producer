"""A broken browser harness cannot stay invisible, without the default suite needing a browser.

Epic 10's retrospective action item **22**: *a gate that is not in the gate is not a gate.*
`pyproject.toml` sets `testpaths = ["tests"]` with pytest's default `python_files`, so all
~~twenty-four~~ ~~twenty-five~~ ~~twenty-six~~ ~~twenty-nine~~ ~~thirty~~ **thirty-one**
`tests/e2e_*.py` files —
~~twenty-three~~ ~~twenty-four~~ ~~twenty-five~~ ~~twenty-eight~~ ~~twenty-nine~~ **thirty**
harnesses
and `e2e_support.py` — sit outside `uv run pytest`. *(Corrected 2026-09-04: this said twenty-six
and twenty-five, and was two harnesses stale before Suggest Video's was added — a count going
stale inside the module whose whole subject is counts going stale, and the one number here that
no test reads. `test_the_runbook_counts_what_it_lists` guards the runbook's sentences; nothing
guards this one.)* What that cost, measured:
`e2e_effects_tab.py` broke on Epic 10's first slice and **four consecutive slices reported green**;
`1933c2e` made `e2e_band_panel.py`'s step 7e unreachable, so Story 10.4's browser QA gate could not
have passed as written; two more have been failing since before Epic 10; and `e2e_band_panel.py`
was missing from `docs/OPERATIONS.md`'s list for three slices, where it landed on a port the
Monitor preview already had.

**The constraint is real and is not being argued with: the default suite must not need a browser.**
These scripts drive Edge, start their own servers and take minutes each. Nothing here runs one.

**What is checked instead is everything about a harness that a browser is not needed to see** —
and every one of these has actually happened here:

| Failure | Caught by |
|---|---|
| A harness stops parsing | `test_every_harness_parses` (and `ruff check .`, measured below) |
| A harness imports a name a sibling no longer exports | `test_every_harness_imports` |
| A harness is not in `docs/OPERATIONS.md`'s runbook | `test_every_harness_is_in_the_runbook` |
| The runbook names a harness that is not there | `test_the_runbook_names_only_harnesses_that_exist` |
| The runbook's port and the script's port disagree | `test_the_runbook_quotes_each_harnesss_own_default_port` |
| A new pair of harnesses collides on a port | `test_no_undeclared_pair_shares_a_default_port` |
| The runbook's own counts go stale | `test_the_runbook_counts_what_it_lists` |

**What it still lets through, stated plainly, because the alternative is implying coverage that is
not there.** This module says a harness is *well formed and findable*. It says nothing about
whether it passes:

- **A harness that imports cleanly and fails on its fourth assertion is invisible here.** That is
  the `e2e_effects_tab.py` failure — a stack-equality predicate meeting a wire that had gained
  `bindings: []` — and it is inside a function this never calls. Two harnesses are failing today
  and this module is green on both.
- **An unreachable step is invisible here.** `1933c2e` did not break `e2e_band_panel.py`'s
  imports; it made a precondition inside step 7e impossible. Nothing short of running it sees that.
- **`selenium` is stubbed for the import.** It is deliberately not in this project's environment —
  the harnesses are run with `uv run --with selenium` — so a name imported *from selenium* that no
  longer exists reads as fine. Names imported from `e2e_support` and from sibling harnesses are
  resolved for real, which is where this repository's cross-harness breakage actually happens.
- **`e2e_first_run.py` is parsed but not imported**, and it is the one harness that would start a
  browser if it were: it has no `main()` and builds its `webdriver.Edge` at module level. The
  structural test below pins that as the single declared exception, so the next harness written
  that way fails rather than quietly joining it.
- `ruff check .` **already rejects a syntax error in these files** — measured 2026-08-28 by
  putting one in `e2e_chip_column_narrow.py`. `test_every_harness_parses` is here because ruff is
  a separate command someone has to choose to run and this one is in the suite that always runs.
"""

from __future__ import annotations

import ast
import importlib
import re
import sys
import types
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
RUNBOOK = REPO / "docs" / "OPERATIONS.md"

#: Not a harness: the shared `ManagedServer`, console gate and settle helpers every script imports.
#: It has no runbook line for the same reason it has no `main()` — there is nothing to run.
SUPPORT = "e2e_support.py"

#: The one harness that does its work at import: no `main()`, and `webdriver.Edge(...)` at module
#: level, so importing it starts Edge. Parsed, never imported. If it is ever given a `main()` this
#: entry should go, and `test_every_harness_is_shaped_like_a_script` will say so by failing.
NOT_IMPORTABLE = "e2e_first_run.py"

#: `uv run --with selenium python tests/e2e_shot_controls.py         # default port 8767`
RUNBOOK_LINE = re.compile(
    r"^uv run .*python (tests/(e2e_[a-z0-9_]+)\.py).*?"
    r"(?:#\s*default port (\d{4})(?P<collides>\s*←\s*collides)?)?$",
    re.MULTILINE,
)

WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "twenty-one": 21, "twenty-two": 22, "twenty-three": 23,
    "twenty-four": 24, "twenty-five": 25, "twenty-six": 26, "twenty-seven": 27,
    "twenty-eight": 28, "twenty-nine": 29, "thirty": 30,
}


def harnesses() -> list[Path]:
    """Every browser harness, in a stable order. Discovered, never listed."""
    return [path for path in sorted(TESTS.glob("e2e_*.py")) if path.name != SUPPORT]


def runbook_entries() -> dict[str, tuple[int | None, bool]]:
    """`module name -> (default port, marked as colliding)` for each runbook line."""
    text = RUNBOOK.read_text(encoding="utf-8")
    found: dict[str, tuple[int | None, bool]] = {}
    for match in RUNBOOK_LINE.finditer(text):
        port = match.group(3)
        found[match.group(2)] = (int(port) if port else None, bool(match.group("collides")))
    return found


def declared_port(path: Path) -> int | None:
    """The `port = 8779` a harness's own `main` starts from, read off its parse tree.

    By name and by parse tree rather than by regex: a script that stopped having a `main` at all
    would still have a four-digit number in its docstring.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "main":
            continue
        for statement in ast.walk(node):
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and statement.targets[0].id == "port"
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, int)
            ):
                return statement.value.value
    return None


class _Stub(Exception):
    """Whatever a harness reaches for inside `selenium`.

    An `Exception` subclass on purpose: it is callable, it can be subclassed, and it can stand in
    an `except` clause — the three things a harness does with a selenium name at import time.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args)

    def __getattr__(self, name: str) -> type[_Stub]:
        return type(name, (_Stub,), {})

    @classmethod
    def __class_getitem__(cls, item: object) -> type[_Stub]:
        return cls


class _StubModule(types.ModuleType):
    def __getattr__(self, name: str) -> type[_Stub]:
        return type(name, (_Stub,), {})


class _SeleniumFinder:
    """Fabricates any `selenium...` module on demand, so an import resolves without the package."""

    def find_module(self, fullname: str, path: object = None) -> _SeleniumFinder | None:
        return self if fullname == "selenium" or fullname.startswith("selenium.") else None

    def find_spec(self, fullname: str, path: object = None, target: object = None):
        if fullname != "selenium" and not fullname.startswith("selenium."):
            return None
        return importlib.machinery.ModuleSpec(fullname, self)

    def create_module(self, spec) -> _StubModule:
        module = _StubModule(spec.name)
        module.__path__ = []  # type: ignore[attr-defined]
        return module

    def exec_module(self, module: types.ModuleType) -> None:
        return None


def test_every_harness_parses():
    """A harness that stopped being Python. Cheapest of these and the one with no exceptions."""
    broken: list[str] = []
    for path in harnesses() + [TESTS / SUPPORT]:
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as error:
            broken.append(f"{path.name}:{error.lineno}: {error.msg}")
    assert not broken, "\n".join(broken)


def test_every_harness_imports():
    """Every harness's module body runs, with `selenium` stubbed and no browser started.

    This is the check that sees a **cross-harness** break, which is the shape this repository
    grows: `e2e_band_panel.py` imports `sidecar`, `targets` and `write_manifest` from
    `e2e_song_analysis.py` and `manifest`, `post_multipart_project` and `select_project` from
    `e2e_timeline_edit.py`, and `e2e_chip_column_narrow.py` imports its fixture from
    `e2e_chip_column.py`. Renaming any one of those in the module that owns it leaves ruff clean
    and the suite green, and breaks a gate nobody would run until the next audit.
    """
    if str(TESTS) not in sys.path:
        sys.path.insert(0, str(TESTS))
    finder = _SeleniumFinder()
    sys.meta_path.insert(0, finder)
    failed: list[str] = []
    try:
        for path in harnesses():
            if path.name == NOT_IMPORTABLE:
                continue
            try:
                importlib.import_module(path.stem)
            except Exception as error:  # noqa: BLE001 - the failure is the result
                failed.append(f"{path.name}: {type(error).__name__}: {error}")
    finally:
        sys.meta_path.remove(finder)
        for name in [name for name in sys.modules if name.split(".")[0] == "selenium"]:
            del sys.modules[name]
    assert not failed, "\n".join(failed)


def test_every_harness_is_shaped_like_a_script():
    """A `main()` behind an `if __name__ == "__main__":`, so importing one costs nothing.

    Not a style rule — it is the precondition for the test above. `e2e_first_run.py` is the one
    exception and is named as one; a second harness written the same way fails here rather than
    being silently dropped from the import check.
    """
    unguarded: list[str] = []
    for path in harnesses():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        has_main = any(
            isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body
        )
        guarded = any(
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            for node in tree.body
        )
        if not (has_main and guarded):
            unguarded.append(path.name)
    assert unguarded == [NOT_IMPORTABLE], unguarded


def test_every_harness_is_in_the_runbook():
    """The omission `docs/OPERATIONS.md` warns about, in the paragraph it warns about it in.

    *"Add the line when you add the script; a gate nobody can find is a gate nobody runs."*
    `e2e_band_panel.py` was written for Epic 10 and left off that list through three slices; two
    more were missing again on 2026-08-25. The warning has now been written three times.
    """
    listed = set(runbook_entries())
    missing = sorted(path.stem for path in harnesses() if path.stem not in listed)
    assert not missing, (
        f"under tests/ and in no run line in {RUNBOOK.name}: {', '.join(missing)}. "
        "Add the line when you add the script."
    )


def test_the_runbook_names_only_harnesses_that_exist():
    """The other direction: a renamed or deleted harness leaves a line that cannot be run."""
    present = {path.stem for path in harnesses()}
    phantom = sorted(name for name in runbook_entries() if name not in present)
    assert not phantom, f"{RUNBOOK.name} tells you to run: {', '.join(phantom)}"


def test_the_runbook_quotes_each_harnesss_own_default_port():
    """`# default port 8779` against the `port = 8779` the script actually starts from.

    The runbook's numbers are what someone reads before running two at once. When they and the
    script disagree, the collision markers below are drawn from the wrong set.
    """
    wrong: list[str] = []
    for path in harnesses():
        documented, _ = runbook_entries().get(path.stem, (None, False))
        actual = declared_port(path)
        if documented is not None and actual is not None and documented != actual:
            wrong.append(f"{path.name} starts on {actual}; the runbook says {documented}")
        if documented is None and actual is not None:
            wrong.append(f"{path.name} starts on {actual} and the runbook line quotes no port")
    assert not wrong, "\n".join(wrong)


def test_no_undeclared_pair_shares_a_default_port():
    """Three pairs collide today and the runbook marks all three. A fourth has to be marked too.

    Not *"no two harnesses share a port"* — that would fail on the day it landed and be deleted.
    `ManagedServer` refuses a bound port by name rather than reusing it, so a collision costs a
    failed start and never a run against the wrong server; what it costs is the two scripts not
    running at the same time, and that is a fact the runbook has to carry. The gate is therefore
    that the marked set and the real set are the same set.
    """
    entries = runbook_entries()
    by_port: dict[int, list[str]] = {}
    for name, (port, _) in entries.items():
        if port is not None:
            by_port.setdefault(port, []).append(name)

    colliding = {port for port, names in by_port.items() if len(names) > 1}
    marked = {port for port, collides in entries.values() if collides and port is not None}
    assert colliding == marked, (
        f"ports that really collide: {sorted(colliding)}; ports the runbook marks "
        f"'← collides': {sorted(marked)}"
    )


def test_the_runbook_counts_what_it_lists():
    """*"These twenty start and prove their own server"* — against the number of lines above it.

    This list *"was five entries long and said 'these five' while there were twelve, which is why
    the ports were never noticed to overlap"*. The sentence is quoting itself; the count is the
    part a machine can hold.
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    self_hosting = sum(1 for port, _ in runbook_entries().values() if port is not None)
    claimed = re.search(r"These ([a-z-]+) \*\*start and prove their own server\*\*", text)
    assert claimed, "the sentence that counts the self-hosting harnesses has moved or changed"
    assert WORDS[claimed.group(1)] == self_hosting, (
        f"the runbook says 'These {claimed.group(1)}' and lists {self_hosting} with a default port"
    )

    pairs = re.search(r"\*\*([A-Za-z-]+) pairs share a default port\*\* — ([0-9, and]+)", text)
    assert pairs, "the sentence that counts the port collisions has moved or changed"
    entries = runbook_entries()
    by_port: dict[int, int] = {}
    for port, _ in entries.values():
        if port is not None:
            by_port[port] = by_port.get(port, 0) + 1
    colliding = sorted(port for port, count in by_port.items() if count > 1)
    assert WORDS[pairs.group(1).lower()] == len(colliding), (
        f"the runbook says '{pairs.group(1)} pairs' and {len(colliding)} ports are shared"
    )
    assert [int(number) for number in re.findall(r"\d{4}", pairs.group(2))] == colliding


@pytest.mark.parametrize("name", [path.stem for path in harnesses()])
def test_each_harness_is_reached_by_all_of_the_above(name):
    """A named case per harness, so a green run says which thirty were looked at.

    Without this the whole of item 22 is seven test functions, and a harness that vanished from
    `tests/` would take its coverage with it silently — which is the failure mode of every count
    this repository has had to correct.
    """
    assert (TESTS / f"{name}.py").exists()
