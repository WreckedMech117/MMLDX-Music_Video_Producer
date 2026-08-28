"""One scan of the package's own source, for the guards that used to scan `app.py` alone.

Not a test module -- a support module, like `race_support.py` and `e2e_support.py`, imported by
the three test files whose source guards live here.

**Why this exists.** Several of this repository's strongest guards are grep-and-count assertions
over source text: exactly two writers of `Asset.name`, exactly five constructions of an `Asset`,
exactly one function that drains ffmpeg's stderr the safe way. Each of them was written when
every route in this application was a nested function inside `create_app`, so each of them was
written as a read of `src/music_video_producer/app.py` -- and the filename was never the point.
The point was always "in this application". Scoping the scan to one file made every one of them
blind to the sibling module a new write path would most plausibly be added to, and that blindness
is the exact shape of the hole this project has now counted twelve times from the other end.

So the scans are widened rather than moved: every `.py` under `src/music_video_producer/`, which
is the whole of this application's server side. A count that was five in one file has to be the
real number over the package, and a sixth anywhere still fails.

**Comments and docstrings are removed first**, for the reason `app_py_saving_routes` gives about
its own scan: several of these routes explain the guard in prose that quotes the guard's own
spelling, and this package's docstrings quote each other's counts. An assertion that matched the
explanation rather than the code would be measuring itself, and a widened scan meets far more
prose than a single file's did. What is stripped is exactly module, class and function
docstrings plus `#` comments; every other string literal -- the route paths, the refusal
sentences -- is code and stays. Stripping blanks the characters in place rather than deleting
lines, so a reported line number still means what it says.

**Functions are found by name across the package, not by slicing between markers.** The tests
that used to take `inspect.getsource(create_app)` and split it on the next `\\n    @app.` were
reading a body whose end was decided by whatever route happened to be declared after it. That is
two accidents deep: the file, and the neighbour. `function_source` walks each module's parse
tree instead, requires the name to be defined exactly once in the whole package, and returns
that definition's own source segment -- so a body ends where the body ends, the guard follows
the function wherever it is declared, and a second definition appearing anywhere is itself a
failure rather than a silent halving of what gets read.
"""

from __future__ import annotations

import ast
import io
import tokenize
from functools import lru_cache
from pathlib import Path

#: The application's server-side source. Every scan below covers all of it and nothing else.
PACKAGE = Path(__file__).resolve().parent.parent / "src" / "music_video_producer"


def package_modules() -> list[Path]:
    """Every module in the package, in a stable order."""
    return sorted(PACKAGE.rglob("*.py"))


def module_name(path: Path) -> str:
    """A module's path *relative to the package*, which is the name a guard should report.

    Not `Path.name`: `timeline.py` and `routes/timeline.py` both exist, and a scan keyed on the
    bare filename collapses them into one entry -- the second silently overwriting the first.
    Two of this package's filenames already collide that way today.
    """
    return path.relative_to(PACKAGE).as_posix()


def _prose_spans(text: str) -> list[tuple[int, int, int, int]]:
    """Where this module's prose is: every `#` comment, and every module/class/function docstring.

    A span is `(start_row, start_col, end_row, end_col)`, rows 1-based, exactly as the tokenizer
    and the parse tree report them. `module_code` blanks these out; `module_prose` keeps only
    these, so the two are complements over the same character grid and a line number read off
    either still means what it says in the file.
    """
    spans: list[tuple[int, int, int, int]] = []
    tree = ast.parse(text)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not body or not isinstance(body[0], ast.Expr):
            continue
        first = body[0].value
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            spans.append((body[0].lineno, body[0].col_offset, body[0].end_lineno, body[0].end_col_offset))
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            spans.append((token.start[0], token.start[1], token.end[0], token.end[1]))
    return spans


def _blank(text: str, spans: list[tuple[int, int, int, int]], *, keep: bool) -> str:
    """`text` with `spans` blanked out (`keep=False`) or with everything *else* blanked
    (`keep=True`). Either way every surviving character keeps its line and column."""
    lines = text.splitlines(keepends=True)
    if keep:
        grid = [[" " if character != "\n" else "\n" for character in line] for line in lines]
    else:
        grid = [list(line) for line in lines]
    for start_row, start_col, end_row, end_col in spans:
        for row in range(start_row, end_row + 1):
            source_line = lines[row - 1]
            line = grid[row - 1]
            first_col = start_col if row == start_row else 0
            last_col = end_col if row == end_row else len(source_line)
            for index in range(first_col, min(last_col, len(line))):
                if source_line[index] != "\n":
                    line[index] = source_line[index] if keep else " "
    return "".join("".join(line) for line in grid)


def module_code(path: Path) -> str:
    """One module's source with its comments and docstrings blanked out.

    Blanked, not deleted: every surviving character keeps its line and column, so a count made
    here and a line number read off the file still describe the same place.
    """
    text = path.read_text(encoding="utf-8")
    return _blank(text, _prose_spans(text), keep=False)


def module_prose(path: Path) -> str:
    """One module's comments and docstrings, with its *code* blanked out — `module_code`'s inverse.

    The two together are the whole file and they do not overlap, which is the property the
    stale-claim scan needs: a sentence found here is prose, and a token found in `module_code`
    is code. "This thing does not exist yet" written in prose about a name that is now in code
    is exactly the shape Epic 10 shipped four times, and it is only separable because these two
    reads are complements rather than two greps over the same text.
    """
    text = path.read_text(encoding="utf-8")
    return _blank(text, _prose_spans(text), keep=True)


@lru_cache(maxsize=1)
def package_code() -> str:
    """Every module's code, concatenated. The text the widened counts are made over."""
    return "\n".join(module_code(path) for path in package_modules())


def count(needle: str) -> int:
    """How many times `needle` appears in the package's code."""
    return package_code().count(needle)


def modules_containing(needle: str) -> dict[str, int]:
    """Which modules' code contains `needle`, and how many times, keyed by `module_name`."""
    found: dict[str, int] = {}
    for path in package_modules():
        occurrences = module_code(path).count(needle)
        if occurrences:
            found[module_name(path)] = occurrences
    return found


def _defined_once(name: str) -> tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Where the one function called `name` is declared, its module's text, and its node.

    Raises if the package declares it zero times or more than once, because both of those make
    every assertion built on what is returned meaningless -- the first vacuously and the second
    by reading one of two implementations that were supposed to be one.
    """
    found: list[tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for path in package_modules():
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
                found.append((module_name(path), text, node))
    assert found, f"the package declares no function called {name}, so this test proves nothing"
    assert len(found) == 1, (
        f"{name} is declared {len(found)} times in the package "
        f"({', '.join(where for where, _, _ in found)}); a guard that reads one of them proves "
        "nothing about the other"
    )
    return found[0]


def function_source(name: str) -> str:
    """The source of the one function called `name`, wherever in the package it is declared."""
    where, text, node = _defined_once(name)
    segment = ast.get_source_segment(text, node)
    assert segment is not None, f"{name} in {where} has no source segment"
    return segment


def function_ast(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The parse-tree node of the one function called `name`, for the guards that walk it."""
    return _defined_once(name)[2]
