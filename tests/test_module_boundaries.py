"""AD-25's leaf modules stop being true by discipline.

Epic 10's retrospective action item **23**: *a prose statement of a rule is not a guard, including
inside source.* Of the four instances it named, three are closed. This is the fourth, and the
retrospective's own words for it are *"true by discipline"* — `effects.py`, `assembly.py` and
`audio.py` import the standard library and nothing from this package, measured 2026-08-28 and
enforced by nothing at all.

**It is load-bearing, and Epic 10 leant on it twice.** The `sendcmd` compiler stayed a pure
function of its arguments because the module it lives in has no manifest to reach for. And a
fingerprint was pinned across a version boundary by importing `effects.py` *from an earlier
commit* — which is only possible for a module that drags no package behind it. Neither of those
survives the first `from .models import Shot` anybody adds, and nothing today would say a word.

**What AD-25 actually requires**, taken from the Rule and its 2026-08-26 amendment rather than
from the summary: `audio.py` — the standard library and `numpy`, the latter by Director ruling
**R-8**, whose evidence was that `uv.lock` gained no `[[package]]` because numpy was already
locked transitively. `effects.py` — *"the standard library and nothing else from this package —
no `models`, no `assembly`"*. `assembly.py` — the same, and it is the module the other two are
forbidden to import. **A fourth is added here that AD-25 does not name**: `h3_prompt.py`, because
`models.py` states the identical rule about it in bold and nothing executed it either.

**What this lets through.** It reads `import` statements, so a module that reached back into the
package through `importlib.import_module("music_video_producer.models")` or through a callable
passed in from a route would pass. It says nothing about *purity*: AD-25's own amendment counted
eleven filesystem call sites in `effects.py` and `audio.py` shells out to ffmpeg, both on purpose.
And it is a rule about the four modules named below only — nothing here prevents a fifth leaf
being written and then quietly growing an edge, because nothing would declare it a leaf.
"""

from __future__ import annotations

import ast
import sys

import pytest
from package_source import PACKAGE, module_name, package_modules

#: The leaf modules AD-25 names, and the non-stdlib import each is allowed. `numpy` in `audio.py`
#: is R-8; the empty sets are the amendment's *"nothing else"*, written as data so that adding an
#: import to one of these files is a change to this line and not just to that file.
LEAVES: dict[str, frozenset[str]] = {
    "effects.py": frozenset(),
    "assembly.py": frozenset(),
    "audio.py": frozenset({"numpy"}),
    #: Not AD-25's, and here because `models.py` states the same rule about it in its own words
    #: and in bold — *"`h3_prompt` is a leaf module — it imports nothing from this package, by
    #: design … **Nothing may ever import `models` from `h3_prompt`.**"* — which was, until this
    #: line, one more constraint written down and not executed. `models.py` imports it, so the
    #: reverse edge is the cycle that sentence exists to forbid.
    "h3_prompt.py": frozenset(),
}

#: Every module this package holds, by its top-level name — what a leaf may not reach for.


def package_module_names() -> set[str]:
    return {module_name(path).removesuffix(".py").split("/")[-1] for path in package_modules()}


def imports(source: str) -> list[tuple[str, int, int]]:
    """`(root module, relative level, line)` for every import in one module's source."""
    found: list[tuple[str, int, int]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [(alias.name.split(".")[0], 0, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            found.append((root, node.level, node.lineno))
    return found


@pytest.mark.parametrize("leaf", sorted(LEAVES))
def test_the_leaf_module_imports_the_standard_library_and_its_one_allowance(leaf):
    """AD-25, executed. `__future__` is the compiler, not a dependency, and is allowed everywhere."""
    path = PACKAGE / leaf
    assert path.exists(), f"{leaf} is what AD-25 is about and it is not here"
    siblings = package_module_names()
    offending: list[str] = []
    for root, level, line in imports(path.read_text(encoding="utf-8")):
        if level:
            offending.append(f"{leaf}:{line} imports relatively from within the package")
        elif root in siblings and root != leaf.removesuffix(".py"):
            offending.append(f"{leaf}:{line} imports `{root}`, a module of this package")
        elif root == "music_video_producer":
            offending.append(f"{leaf}:{line} imports the package by name")
        elif root not in sys.stdlib_module_names and root not in LEAVES[leaf] | {"__future__"}:
            offending.append(f"{leaf}:{line} imports `{root}`, which is neither stdlib nor allowed")
    assert not offending, "\n".join(offending)


def test_the_allowances_are_the_ones_the_architecture_records():
    """The exemption list is itself a claim, so it is pinned rather than trusted.

    `numpy` in `audio.py` is the only third-party import any of the four carries, and it exists
    because a Director ruling (R-8) amended FX-NFR-4's *"no package is added to the runtime
    dependency set"* after the evidence was checked. Widening this dict is how that decision
    would be undone by accident; making it fail is how it stays a decision.
    """
    assert {leaf: sorted(allowed) for leaf, allowed in LEAVES.items()} == {
        "assembly.py": [],
        "audio.py": ["numpy"],
        "effects.py": [],
        "h3_prompt.py": [],
    }


def test_the_leaves_are_reached_from_the_package_so_the_rule_is_not_vacuous():
    """A module nobody imports is trivially a leaf. These four are load-bearing, not orphaned.

    Direction matters and only one direction is a violation: the package importing a leaf is the
    architecture working (`app.py` and `routes/shots.py` call `effects.drive_readout`); a leaf
    importing the package is the architecture gone. This asserts the first, so that a green run
    on the test above means *"these four carry weight and take none"* rather than *"these four
    are unused"*.
    """
    unreached = []
    for leaf in sorted(LEAVES):
        name = leaf.removesuffix(".py")
        importers = [
            module_name(path)
            for path in package_modules()
            if module_name(path) != leaf
            and any(root == name for root, _, _ in imports(path.read_text(encoding="utf-8")))
        ]
        if not importers:
            unreached.append(leaf)
    assert not unreached, f"nothing in the package imports {', '.join(unreached)}"
