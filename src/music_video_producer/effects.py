r"""The one place a derived artefact is tied to the thing it was derived from.

AD-28 asks for *one* fingerprint function rather than an ad-hoc hash per consumer, and the
reason is that a fingerprint is only useful if every writer and every reader compute it the
same way. Two hashes that disagree do not report "stale" — they report it inconsistently,
which is worse than not checking at all.

Today there is exactly one: `song_fingerprint`. The module is created now, holding only that,
because the Song Envelope needs it and the later effects work (grades, chains, previews) will
add its siblings here rather than inventing a second convention.

**Content, never mtime.** A modification time changes when a file is copied, restored from a
backup, or synced — and does *not* change when a file is edited in place with the timestamp
preserved. Both directions are wrong for the question being asked, which is "is this still the
same audio?". Size plus a hash of the bytes answers exactly that, and the two are composed in a
fixed order so the string is reproducible: size first, then the digest.

The bytes are read in chunks rather than whole. A master is a few megabytes today, but this
function has no business deciding how large a song is allowed to be, and a streaming read costs
nothing to write.

---

**The effects chain** (AD-17, AD-25, AD-27, AD-31) lives here too, and it is the second half of
this module's job: a pure function from an Effect Stack to the ordered filter stages
`assembly.trim_args` splices into its chain. Nothing here imports `app.py`, `batch.py` or
`assembly.py`; every stage it emits is a string, compared as a string in the tests, exactly as
this application already treats its ffmpeg argv.

Four things live below, in the order a stack passes through them:

1. **The LUT folder.** `{data_root}/luts/`, a sibling of `projects/` and
   `machine-preferences.json`. Looks are *discovered*, never bundled: this repository ships
   nobody else's colour work, and a default set is generated on first use so a fresh install
   still has something to grade with. A client names a LUT by an id derived from what the server
   discovered; a client-supplied string never becomes a path.
2. **The catalogue.** Server-side data — which effects exist, which family each is in, what
   parameters each declares, the bounds and default of every one, and how each composes into
   filter text.
3. **The validator.** The only thing that decides whether a value may reach a filter string. A
   stack in, and either agreement or a refusal naming the offending effect, parameter and bound.
4. **The chain builder.** A stack and the export's geometry in, two ordered stage groups out: the
   ones that go before `scale`, and the ones that go before `pad`.

**The stage order is fixed and is not the Director's to reorder:**

```
trim -> GEOMETRY -> scale -> TEXTURE -> GRADE -> STYLIZE -> pad -> fps -> setsar -> format
```

Within a family the Director's order is kept. The builder **sorts by family as it reads**
(AD-31), so a stack stored out of order — copied, hand-edited, written by an older client — is
harmless rather than undefined. Storage order is never load-bearing.

Two ordering constraints carry measurements rather than opinions. **Geometry precedes `scale`**
so a punch-in samples the take's own pixels instead of resampling an already-scaled frame — the
one constraint that is invisible in a still and obvious in motion. **Every treatment precedes
`pad`**: measured 2026-08-21 on a 4:3 source into a 16:9 target, texture after `pad` leaves the
letterbox bar at RGB `(1,1,5)` and before `pad` at `(0,0,0)`.

**How a LUT path reaches `lut3d`, measured 2026-08-25 against this project's ffmpeg 7.0.** An
absolute Windows path fails, and the error names the wrong thing entirely — `Error applying
option 'clut' to filter 'lut3d': Invalid argument`, mentioning neither the path nor the real
problem, because the filtergraph parser splits at the drive-letter colon and reads the tail as a
positional option. Every form below was tried against directory names holding a space, a comma,
a semicolon, brackets, an equals sign and an apostrophe:

| Form | Result |
| --- | --- |
| `C:/x/y.cube`, and the same with backslashes | fails always |
| `C\\\:/x/y.cube` (unquoted, escaped) | works — until the path holds a `,` or a `;` |
| a bare relative path with the process cwd set | works — until the path holds a `,` or a `;` |
| `'C\:/x/y.cube'` (single-quoted, colon escaped) | **works for every case but one** |

So the quoted form is what this module writes. It is strictly more robust than the cwd-relative
remedy `sendcmd` is documented to need (`docs/BUILD-HANDOFF.md`), and it needs no working-
directory contract at all, which is why the export path is left untouched by this work. The one
case nothing survives is an **apostrophe in the path**: no escaping of any kind reaches the file,
so a LUT under such a directory is refused by name here rather than failing inside ffmpeg with a
message about `clut`.

**Three `.cube` facts the generator honours, each of which fails silently if got wrong.** Red
varies fastest, then green, then blue — written as nested loops the *outer* one is blue. Measured
here: a generated identity round-trips at **84.2 dB** PSNR; the same identity written
blue-fastest scores **3.73 dB**, with red and blue swapped and green untouched. `DOMAIN_MIN
0 0 0` and `DOMAIN_MAX 1 1 1` are always written, because ffmpeg computes
`scale = clip(1/(max-min), 0, 1)` and never subtracts `min`, so a `DOMAIN_MIN` offset is ignored
and a `DOMAIN_MAX` below 1 is silently clamped away. And `LUT_3D_SIZE N` is the only mandatory
line — everything before it is ignored, and the parser wants exactly one space after each header
keyword.

The grid is **33 cubed**: measured during research at 330 ms per 120 1080p frames against 319 ms
for 17 cubed, so the finer lattice is very nearly free, while 17 cubed visibly quantises
gradients.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_LUTS",
    "DEFAULT_LUT_SIZE",
    "EFFECT_CATALOGUE",
    "FAMILY_GEOMETRY",
    "FAMILY_GRADE",
    "FAMILY_ORDER",
    "FAMILY_STYLIZE",
    "FAMILY_TEXTURE",
    "FINGERPRINT_CHUNK_BYTES",
    "LUT_DIRECTORY_NAME",
    "LUT_HEADER_SCAN_BYTES",
    "LUT_SUFFIX",
    "PRE_PAD_FAMILIES",
    "PRE_SCALE_FAMILIES",
    "ChoiceParameter",
    "EffectDefinition",
    "EffectRefusal",
    "EffectStages",
    "LutEntry",
    "LutParameter",
    "NumberParameter",
    "ResolvedEffect",
    "StageContext",
    "build_effect_stages",
    "cube_text",
    "discover_luts",
    "fingerprint_size",
    "identity_transform",
    "lut_directory",
    "lut_file_argument",
    "lut_id_for_name",
    "song_fingerprint",
    "song_fingerprints_match",
    "validate_stack",
    "write_default_luts",
]

#: Read size for the streaming hash. One mebibyte: large enough that the loop is not the cost,
#: small enough that a pathological file never becomes a memory decision.
FINGERPRINT_CHUNK_BYTES = 1 << 20


def song_fingerprint(path: Path | str) -> str:
    """A content fingerprint of one audio file: `"{size}-{sha256}"`, or `""` if unreadable.

    The empty string is deliberately **not** a fingerprint. It means "no fingerprint could be
    taken", and every caller must treat it as a mismatch rather than as a value that could
    happen to be equal to a stored one — which is why `song_fingerprints_match` exists beside
    this and is what comparisons should go through.

    Ordered inputs, stated because the order is the contract: the byte length of the file, then
    the SHA-256 of its bytes. Size alone is cheap and weak; the digest alone is strong and says
    nothing about truncation. Both, in this order, is what every reader recomputes.
    """
    try:
        source = Path(path)
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as handle:
            while chunk := handle.read(FINGERPRINT_CHUNK_BYTES):
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        # A missing or unreadable song is not an error here. It is the answer "nothing can be
        # said about this file", and the caller's job is to report the derived thing absent —
        # never to raise out of a read-time validity check.
        return ""
    return f"{size}-{digest.hexdigest()}"


def fingerprint_size(fingerprint: str) -> int | None:
    """The byte count a fingerprint was taken over, or `None` if it is not a fingerprint.

    The reason the size is the *first* half of the string and not an implementation detail buried
    in a digest: it can be compared against a file without reading the file. `Path.stat()` is a
    metadata lookup; the SHA-256 beside it reads every byte of a master, which on a 10 MB track is
    tens of milliseconds and on the render-status poll would be tens of milliseconds every two
    seconds, forever, to answer "unchanged" every time.

    So this is the cheap half of a two-stage comparison. A size that differs is a definitive
    "these are not the same bytes" and no hash is needed; a size that matches is only a *maybe*,
    and the digest settles it. Same-size different-content is entirely possible — an edit in
    place, a re-render of the same length — which is why this may never be the whole check.
    """
    head, _, rest = fingerprint.partition("-")
    if not rest or not head.isdigit():
        return None
    return int(head)


def song_fingerprints_match(stored: str, current: str) -> bool:
    """Whether a stored fingerprint still describes the song in front of us.

    Equality is necessary and not sufficient: two empty strings are equal and mean *neither*
    could be measured, so a project whose song file has gone missing must not be told its
    analysis is current. Requiring both to be non-empty is the whole of the difference, and it
    is here rather than at each call site because "== " is exactly the mistake this prevents.
    """
    return bool(stored) and bool(current) and stored == current


# ------------------------------------------------------------------------------------------
# Families, and the one fixed stage order (AD-17).
#
# The order below is the whole of the ordering contract. It is a tuple rather than a set of
# comparisons scattered through the builder so that changing it is one edit in one place — and
# so that a test can assert the order itself rather than a consequence of it.
# ------------------------------------------------------------------------------------------

FAMILY_GEOMETRY = "geometry"
FAMILY_TEXTURE = "texture"
FAMILY_GRADE = "grade"
FAMILY_STYLIZE = "stylize"

#: `trim -> GEOMETRY -> scale -> TEXTURE -> GRADE -> STYLIZE -> pad -> ...`. Not the Director's
#: to reorder; within a family, order *is* theirs.
FAMILY_ORDER: tuple[str, ...] = (
    FAMILY_GEOMETRY,
    FAMILY_TEXTURE,
    FAMILY_GRADE,
    FAMILY_STYLIZE,
)

#: Geometry alone runs before `scale`, so a punch-in samples the take's own pixels rather than
#: resampling a frame that has already been resized to the export's grid.
PRE_SCALE_FAMILIES: tuple[str, ...] = (FAMILY_GEOMETRY,)

#: Everything else runs after `scale` and before `pad`, so grain and vignette treat the picture
#: and leave the letterbox bars at pure black. Measured, not assumed — see the module docstring.
PRE_PAD_FAMILIES: tuple[str, ...] = (FAMILY_TEXTURE, FAMILY_GRADE, FAMILY_STYLIZE)

assert set(PRE_SCALE_FAMILIES) | set(PRE_PAD_FAMILIES) == set(FAMILY_ORDER)


class EffectRefusal(ValueError):
    """A stack this application will not compose, with a sentence saying exactly why.

    Every message names the offender: the effect, and where a parameter is at fault, the
    parameter and the bound it broke. It is a refusal rather than a silent skip because an
    effect that quietly does nothing is the failure mode this whole guard exists to prevent —
    a Director would see an ungraded export and have nothing to read.
    """


# ------------------------------------------------------------------------------------------
# Refusal wordings. Kept as constants, in `assembly.py`'s idiom, so a test asserts the sentence
# a Director reads rather than a substring that happens to be in it today.
# ------------------------------------------------------------------------------------------

EFFECT_NOT_A_SPEC_REFUSAL = (
    "An effect must be named with its parameters, and entry {index} of this stack is "
    "{value!r} instead. Nothing was composed."
)
EFFECT_MISSING_ID_REFUSAL = (
    "Entry {index} of this stack names no effect. Every effect is named by its catalogue id. "
    "Nothing was composed."
)
EFFECT_UNKNOWN_REFUSAL = (
    "There is no effect called {effect!r} in the catalogue. Nothing was composed."
)
EFFECT_PARAMETERS_NOT_A_MAP_REFUSAL = (
    "{effect}'s parameters must be given by name, and {value!r} is not. Nothing was composed."
)
EFFECT_ENABLED_NOT_A_FLAG_REFUSAL = (
    "{effect} is either enabled or it is not, and {value!r} is neither. Nothing was composed."
)
EFFECT_UNKNOWN_PARAMETER_REFUSAL = (
    "{effect} has no parameter called {parameter!r}. It takes {declared}. Nothing was composed."
)
EFFECT_PARAMETER_TYPE_REFUSAL = (
    "{effect}'s {parameter} must be {expected}, and {value!r} is not. Nothing was composed."
)
EFFECT_PARAMETER_BELOW_REFUSAL = (
    "{effect}'s {parameter} is {value}, below its minimum of {bound}. Nothing was composed."
)
EFFECT_PARAMETER_ABOVE_REFUSAL = (
    "{effect}'s {parameter} is {value}, above its maximum of {bound}. Nothing was composed."
)
EFFECT_PARAMETER_CHOICE_REFUSAL = (
    "{effect}'s {parameter} must be one of {choices}, and {value!r} is not. "
    "Nothing was composed."
)
EFFECT_LUT_UNNAMED_REFUSAL = (
    "{effect} needs a look chosen. Pick one of the LUTs in the looks folder. "
    "Nothing was composed."
)
EFFECT_LUT_UNKNOWN_REFUSAL = (
    "There is no look called {lut!r} in the looks folder. Nothing was composed."
)
EFFECT_LUT_FILE_MISSING_REFUSAL = (
    "The look {lut!r} is no longer in the looks folder: {path} has gone. Put the file back or "
    "choose another look. Nothing was composed."
)
EFFECT_LUT_PATH_UNUSABLE_REFUSAL = (
    "The look {lut!r} cannot be loaded because its path contains an apostrophe, which ffmpeg's "
    "filter parser has no escape for: {path}. Rename the folder or move the looks elsewhere. "
    "Nothing was composed."
)


# ------------------------------------------------------------------------------------------
# The LUT folder: discovery, stable ids, and a generated default set.
#
# `{data_root}/luts/`, a sibling of `projects/` and `machine-preferences.json` — `preferences.py`
# is the precedent for machine-level state living beside the projects rather than inside one.
# Looks belong to the machine, not to a video: a manifest that carried its own colour science
# would grade differently on someone else's install.
# ------------------------------------------------------------------------------------------

#: The folder's name under the data root. One word, so a Director who finds it in a backup can
#: tell what it is.
LUT_DIRECTORY_NAME = "luts"

#: The only extension offered. Anything else in the folder is somebody's notes, a README, a
#: `.3dl` this application cannot read — ignored, not offered, and never a crash.
LUT_SUFFIX = ".cube"

#: How far into a file the `LUT_3D_SIZE` header is looked for. ffmpeg ignores everything before
#: that line, so in principle it can sit anywhere; in practice it is in the first few lines, and
#: an unbounded scan would mean reading every megabyte of every file in the folder to answer
#: "what looks are available?". Eight kibibytes is generous for a header and cheap for a listing.
LUT_HEADER_SCAN_BYTES = 8192

#: The lattice size the generated defaults are written at. See the module docstring: 33 costs
#: 330 ms per 120 1080p frames against 319 ms for 17, and 17 visibly quantises gradients.
DEFAULT_LUT_SIZE = 33


@dataclass(frozen=True, slots=True)
class LutEntry:
    """One look the server found in the folder: the id a client may name, and the file it is.

    The id is derived from the filename by the server, and the path is never reconstructed from
    anything a client sent. That separation is the whole security property of the Grade family:
    a client picks from a list the server produced, and a client string is compared against that
    list rather than joined onto a directory.
    """

    lut_id: str
    name: str
    path: Path


def lut_directory(data_root: Path | str) -> Path:
    """`{data_root}/luts` — beside `projects/`, never inside one."""
    return Path(data_root) / LUT_DIRECTORY_NAME


def lut_id_for_name(name: str) -> str:
    """A stable id for one discovered file: lowercase, hyphenated, alphanumerics only.

    Derived from the filename rather than stored, so a folder is the whole of the state: drop a
    file in, it is offered; take it out, it is gone. The transformation is deliberately lossy —
    two files whose names differ only in punctuation collapse to the same id — and
    `discover_luts` resolves that collision by suffixing, in the folder's sorted order, so the
    ids a folder produces are the same ids on every run and on every machine.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "lut"


def _clamp_unit(value: float) -> float:
    """Into 0..1. A `.cube` outside the domain is not an error to ffmpeg; it is a clipped
    highlight nobody asked for, which is worse because it looks like a decision."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def identity_transform(red: float, green: float, blue: float) -> tuple[float, float, float]:
    """The look that changes nothing — the reference the generator's round-trip is asserted on.

    It exists because the one mistake a `.cube` writer makes silently is the loop nesting. An
    identity written red-fastest round-trips at ~84 dB PSNR; the same table written blue-fastest
    scores ~3.7 dB with red and blue swapped, and *no error is reported by anything*. The only
    way to know which one was written is to render an identity and measure it.
    """
    return (red, green, blue)


def _s_curve(value: float, strength: float) -> float:
    """Smoothstep blended toward identity by `strength`. Contrast without a clipped toe."""
    smooth = value * value * (3.0 - 2.0 * value)
    return value + (smooth - value) * strength


def _rec709_luma(red: float, green: float, blue: float) -> float:
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _filmic_transform(red: float, green: float, blue: float) -> tuple[float, float, float]:
    """A gentle S-curve on each channel: shadows down, highlights up, midtones untouched."""
    return (_s_curve(red, 0.45), _s_curve(green, 0.45), _s_curve(blue, 0.45))


def _teal_orange_transform(red: float, green: float, blue: float) -> tuple[float, float, float]:
    """Split-tone: shadows toward teal, highlights toward orange. The grade everyone recognises
    and nobody can name, and the reason a bundled set is worth generating at all."""
    luma = _rec709_luma(red, green, blue)
    shadow = 1.0 - luma
    return (
        red + 0.12 * luma - 0.05 * shadow,
        green + 0.02 * luma + 0.02 * shadow,
        blue - 0.10 * luma + 0.14 * shadow,
    )


def _bleach_bypass_transform(red: float, green: float, blue: float) -> tuple[float, float, float]:
    """Silver retained: two-thirds of the saturation gone and the contrast steepened."""
    luma = _rec709_luma(red, green, blue)
    return (
        _s_curve(red + (luma - red) * 0.65, 0.85),
        _s_curve(green + (luma - green) * 0.65, 0.85),
        _s_curve(blue + (luma - blue) * 0.65, 0.85),
    )


def _warm_shift_transform(red: float, green: float, blue: float) -> tuple[float, float, float]:
    """Tungsten warmth without a colour cast in the blacks — a gain, not a lift."""
    return (red * 1.08, green * 1.01, blue * 0.90)


def _panchromatic_transform(red: float, green: float, blue: float) -> tuple[float, float, float]:
    """Black and white on panchromatic film weights, not the video luma ones: a deeper red
    response, which is what makes skin read as film rather than as a desaturated frame."""
    grey = 0.30 * red + 0.59 * green + 0.11 * blue
    return (grey, grey, grey)


#: The generated default set, written when the folder does not exist. Five looks, no third-party
#: colour science, no licence to read: every one of them is a few lines of arithmetic above.
#: The tuple order is the order they are written; discovery sorts by filename regardless.
LutTransform = Callable[[float, float, float], tuple[float, float, float]]

DEFAULT_LUTS: tuple[tuple[str, str, LutTransform], ...] = (
    ("filmic-contrast", "Filmic Contrast", _filmic_transform),
    ("teal-and-orange", "Teal and Orange", _teal_orange_transform),
    ("bleach-bypass", "Bleach Bypass", _bleach_bypass_transform),
    ("warm-shift", "Warm Shift", _warm_shift_transform),
    ("panchromatic-mono", "Panchromatic Mono", _panchromatic_transform),
)


def cube_text(size: int, transform: LutTransform, *, title: str = "") -> str:
    """One `.cube` file as text — pure, so the generator is asserted by comparison like everything
    else that drives a render.

    Three facts about the format are load-bearing and each fails *silently* if got wrong:

    * **Red varies fastest, then green, then blue.** Written as nested loops the outer one is
      blue. Get it backwards and ffmpeg reports nothing at all; the picture simply comes back
      with its red and blue channels exchanged.
    * **`DOMAIN_MIN 0 0 0` and `DOMAIN_MAX 1 1 1` are always written.** ffmpeg computes
      `scale = clip(1/(max-min), 0, 1)` and never subtracts `min`, so a `DOMAIN_MIN` offset is
      quietly ignored and a `DOMAIN_MAX` under 1 is clamped away. Writing the identity domain is
      the only way the file means what it says.
    * **`LUT_3D_SIZE N` is the only mandatory line.** Everything before it is ignored — which is
      why `TITLE` may lead — and the parser wants exactly one space after each header keyword.

    Values are clamped into the domain and written to six decimals: far finer than the eight-bit
    picture needs, and fixed-width so two runs of this function produce identical bytes.
    """
    if size < 2:
        raise ValueError(f"A 3D LUT needs at least two points per axis, not {size}.")
    lines: list[str] = []
    if title:
        lines.append(f'TITLE "{title}"')
    lines.append(f"LUT_3D_SIZE {size}")
    lines.append("DOMAIN_MIN 0 0 0")
    lines.append("DOMAIN_MAX 1 1 1")
    last = size - 1
    for blue_step in range(size):
        blue = blue_step / last
        for green_step in range(size):
            green = green_step / last
            for red_step in range(size):
                mapped = transform(red_step / last, green, blue)
                lines.append(" ".join(f"{_clamp_unit(value):.6f}" for value in mapped))
    return "\n".join(lines) + "\n"


def write_default_luts(directory: Path, *, size: int = DEFAULT_LUT_SIZE) -> tuple[Path, ...]:
    """Write the generated default set into `directory`, creating it, and never overwriting.

    Never overwriting is the point. A Director who edits or replaces `warm-shift.cube` has made
    a decision, and a generator that reasserted itself on the next start would undo it without
    saying so. The absence of a *file* is what triggers a write, and only for that file.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for lut_id, title, transform in DEFAULT_LUTS:
        destination = directory / f"{lut_id}{LUT_SUFFIX}"
        if destination.exists():
            continue
        destination.write_text(cube_text(size, transform, title=title), encoding="utf-8")
        written.append(destination)
    return tuple(written)


def _looks_like_a_cube(path: Path) -> bool:
    """Whether a `.cube` file actually carries the one line ffmpeg requires.

    A folder is a place people put things. The extension check alone would offer a half-copied
    download or a text file someone renamed, and the Director would find out at export. Reading
    a bounded head and looking for the mandatory header answers it now, for the price of one
    small read per file.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(LUT_HEADER_SCAN_BYTES)
    except OSError:
        return False
    return b"LUT_3D_SIZE" in head


def discover_luts(
    data_root: Path | str, *, generate_defaults: bool = True
) -> tuple[LutEntry, ...]:
    """Every look the folder holds, in filename order, each with the id a client may name.

    Generation happens only when the **folder** is absent, which is the first-run case. A folder
    that exists and is empty is a Director who deleted the defaults, and regenerating them would
    be this application arguing with them. A Director's own files sit beside the generated ones
    and are indistinguishable to the chain — that is the whole design, and it is what dissolves
    the licensing question rather than managing it.

    Ordering is by lowercased filename so the id collision suffixes below are the same on every
    machine, and so a listing does not reshuffle between calls.
    """
    directory = lut_directory(data_root)
    if generate_defaults and not directory.exists():
        try:
            write_default_luts(directory)
        except OSError:
            # A read-only or unwritable data root is not a reason to fail a listing. It means
            # there are no looks, which is a state the chain already reports by name.
            return ()
    try:
        candidates = sorted(
            (path for path in directory.iterdir() if path.suffix.lower() == LUT_SUFFIX),
            key=lambda path: path.name.lower(),
        )
    except OSError:
        return ()
    entries: list[LutEntry] = []
    taken: set[str] = set()
    for path in candidates:
        if not path.is_file() or not _looks_like_a_cube(path):
            continue
        base = lut_id_for_name(path.stem)
        lut_id = base
        suffix = 2
        while lut_id in taken:
            lut_id = f"{base}-{suffix}"
            suffix += 1
        taken.add(lut_id)
        entries.append(LutEntry(lut_id=lut_id, name=path.stem, path=path))
    return tuple(entries)


def lut_file_argument(path: Path, *, lut_id: str = "") -> str:
    r"""The exact text that goes after `lut3d=file=`, for a path this machine actually holds.

    Single-quoted with the drive-letter colon escaped: `'C\:/looks/warm.cube'`. Measured against
    this project's ffmpeg 7.0 on 2026-08-25 — see the module docstring for the full table. Every
    unquoted form, and the cwd-relative form `sendcmd` is documented to need, breaks on a path
    containing a comma or a semicolon; this one survives spaces, commas, semicolons, brackets,
    percent signs, ampersands and equals signs.

    The single case nothing survives is an apostrophe, because ffmpeg's quoting has no escape
    that reaches the file inside a filter option. That is refused by name here rather than left
    to fail inside ffmpeg, whose message for it names `clut` and mentions neither the path nor
    the problem.

    Backslashes cannot appear in a Windows filename, and `as_posix` removes the separators, so
    the check below is a guard against a caller handing this function something that is not a
    path at all rather than against a real filename.
    """
    text = path.as_posix()
    if "'" in text or "\\" in text:
        raise EffectRefusal(
            EFFECT_LUT_PATH_UNUSABLE_REFUSAL.format(lut=lut_id or path.stem, path=text)
        )
    return "'" + text.replace(":", "\\:") + "'"


# ------------------------------------------------------------------------------------------
# The catalogue: server-side data, not a client contract.
#
# What an effect is, which family it belongs to, what parameters it declares, the bounds and
# default of each, and how it composes into filter text. A client sends an id and a handful of
# numbers; everything that decides what ffmpeg actually reads lives here.
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NumberParameter:
    """A bounded number. `integer` means the filter takes a count of pixels or a seed, not a
    fraction, and a value that is not whole is refused rather than rounded — rounding would make
    two different specs produce the same chain, which a fingerprint would then disagree about."""

    name: str
    label: str
    default: float
    minimum: float
    maximum: float
    integer: bool = False


@dataclass(frozen=True, slots=True)
class ChoiceParameter:
    """One of a fixed set of words. The words are filter vocabulary, so the set *is* the
    validation: nothing outside it can reach a filter string."""

    name: str
    label: str
    default: str
    choices: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LutParameter:
    """A look, named by an id the server derived from its own folder.

    It declares no default, and that is deliberate: every other parameter has a value that means
    "leave it alone", and a grade with no look chosen has nothing to apply. Omitting it is
    refused by name rather than silently resolved to whichever file happened to sort first.
    """

    name: str
    label: str


Parameter = NumberParameter | ChoiceParameter | LutParameter


@dataclass(frozen=True, slots=True)
class StageContext:
    """What a composer is allowed to know: the geometry the **export** chose, and the file
    argument for every look the stack named.

    The dimensions are the export's, never the take's, because every stage a composer emits into
    the treatment group runs *after* `scale` — the frame is already the export's size by then.
    Geometry composers run before `scale` and therefore address the take's pixels through ffmpeg's
    own `iw`/`ih`, which is why none of them read these numbers.
    """

    width: int
    height: int
    lut_arguments: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EffectDefinition:
    """One catalogue entry. `compose` is a pure function of validated values and the context."""

    effect_id: str
    family: str
    label: str
    parameters: tuple[Parameter, ...]
    compose: Callable[[Mapping[str, Any], StageContext], tuple[str, ...]]


def _number(value: float) -> str:
    """A float as the shortest text that means it, deterministically.

    Six decimals then stripped, so `1.0` is `1` and `0.25` is `0.25` and neither depends on the
    platform's float repr. Negative zero is normalised, because `-0` and `0` are the same number
    and must not be two different filter strings — a fingerprint over a chain would call those
    two states different and re-render for nothing.
    """
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


# --- Geometry: before `scale`, addressing the take's own pixels through `iw`/`ih`. ---


def _compose_punch_in(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """A centred crop by `zoom`, which the chain's own `scale` then brings back up.

    The zoom is bounded at 1 from below, so the crop is always *inside* the source frame and can
    never expose an undefined edge — FX-11's bound, expressed as the parameter's minimum rather
    than as a clamp buried in this function.
    """
    zoom = _number(values["zoom"])
    return (f"crop=w=iw/{zoom}:h=ih/{zoom}:x=(iw-ow)/2:y=(ih-oh)/2",)


def _compose_dutch_tilt(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    r"""Rotate, then crop back to the largest inscribed rectangle of the same aspect.

    The crop is what keeps the tilt from exposing the black corners `rotate` fills in. Its factor
    is written as an ffmpeg expression over `iw`/`ih` rather than computed here, because this
    stage runs *before* `scale` and therefore has no idea what shape the take is — the export's
    geometry in the context describes the frame this stage's output will later be scaled into,
    not the frame it is cropping.

    The comma inside `max()` is escaped, because the chain these stages join is comma-separated.
    """
    radians = math.radians(float(values["angle"]))
    cosine = _number(abs(math.cos(radians)))
    sine = _number(abs(math.sin(radians)))
    inscribed = f"max((iw*{cosine}+ih*{sine})/iw\\,(iw*{sine}+ih*{cosine})/ih)"
    return (
        f"rotate=a={_number(radians)}:ow=iw:oh=ih",
        f"crop=w=iw/{inscribed}:h=ih/{inscribed}:x=(iw-ow)/2:y=(ih-oh)/2",
    )


def _compose_mirror(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """`hflip`, `vflip`, or both — the one effect here whose default is not a visual no-op,
    because a mirror that mirrors nothing is not a look anybody asked for."""
    axis = values["axis"]
    if axis == "horizontal":
        return ("hflip",)
    if axis == "vertical":
        return ("vflip",)
    return ("hflip", "vflip")


def _compose_handheld_shake(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """A crop of constant size whose *position* moves with time.

    Constant size is the whole trick: `crop` evaluates `x` and `y` for every frame but fixes the
    output dimensions once, so a moving window costs nothing and cannot produce a frame of a
    different shape. The window is inset by the amplitude on all four sides, so the offset can
    never reach outside the source — the same bound `punch_in` gets, by construction rather than
    by clamping.

    The vertical frequency is offset from the horizontal by an irrational-ish ratio so the two
    axes do not return to the same place together and the motion does not read as a circle.
    """
    amplitude = float(values["amplitude"])
    frequency = _number(values["frequency"])
    vertical_frequency = _number(float(values["frequency"]) * 1.37)
    window = _number(1.0 - 2.0 * amplitude)
    swing = _number(amplitude)
    stage = (
        f"crop=w=iw*{window}:h=ih*{window}"
        f":x=(iw-ow)/2+iw*{swing}*sin(2*PI*{frequency}*t)"
        f":y=(ih-oh)/2+ih*{swing}*cos(2*PI*{vertical_frequency}*t)"
    )
    return (stage,)


# --- Texture: after `scale`, before `pad`. The frame is already the export's size here. ---


def _compose_grain(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Temporal grain with a pinned seed.

    The seed is a parameter rather than a constant because two shots carrying the same grain
    should be allowed to differ — but it is *always written*, because `noise` without one is
    seeded from the clock and the same manifest would render a different file every time. That
    would break the standing rule that a render input is a pure function of the manifest.
    """
    strength = _number(values["strength"])
    seed = _number(values["seed"])
    return (f"noise=alls={strength}:allf=t+u:all_seed={seed}",)


def _compose_vignette(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Corner falloff computed against the picture, because this runs before `pad`. An angle of
    zero is the no-op the parameter defaults to."""
    return (f"vignette=angle={_number(values['angle'])}",)


def _compose_soft_focus(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """A Gaussian defocus. Sigma zero is the identity."""
    return (f"gblur=sigma={_number(values['sigma'])}",)


def _compose_sharpen(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Unsharp mask on luma. Negative amounts soften, which is why the range crosses zero."""
    return (f"unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount={_number(values['amount'])}",)


def _compose_banding_suppression(
    values: Mapping[str, Any], context: StageContext
) -> tuple[str, ...]:
    """`deband` with one threshold across all four planes.

    The parameter is the filter's own threshold rather than a friendly 0..1 dial, so what the
    catalogue declares and what ffmpeg receives are the same number. Its minimum is `deband`'s
    own floor, which is visually nothing — the no-op this family's defaults promise.
    """
    threshold = _number(values["threshold"])
    return (f"deband=1thr={threshold}:2thr={threshold}:3thr={threshold}:4thr={threshold}",)


# --- Grade. ---


def _compose_lut_look(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """The only stage that names a file, and the only one that could ever have carried a
    client-supplied path. It does not: the id was matched against the server's own discovery and
    the argument was built from the `Path` that discovery returned."""
    argument = context.lut_arguments[values["lut"]]
    return (f"lut3d=file={argument}:interp={values['interp']}",)


def _compose_exposure(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    return (f"eq=brightness={_number(values['amount'])}",)


def _compose_contrast(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    return (f"eq=contrast={_number(values['amount'])}",)


def _compose_saturation(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    return (f"eq=saturation={_number(values['amount'])}",)


def _compose_temperature(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Warm and cool as one axis: red up and blue down together, in the midtones only, so the
    black point does not take a cast."""
    amount = float(values["amount"])
    return (f"colorbalance=rm={_number(amount)}:bm={_number(-amount)}",)


def _compose_tint(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """The axis perpendicular to temperature: green against magenta."""
    return (f"colorbalance=gm={_number(values['amount'])}",)


def _compose_lift_gamma_gain(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Two stages, because the three controls do not live in one ffmpeg filter: shadows and
    highlights are `colorbalance`, and the midtone curve between them is `eq`'s gamma."""
    lift = _number(values["lift"])
    gain = _number(values["gain"])
    return (
        f"colorbalance=rs={lift}:gs={lift}:bs={lift}:rh={gain}:gh={gain}:bh={gain}",
        f"eq=gamma={_number(values['gamma'])}",
    )


def _compose_monochrome(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Saturation removed by `amount`. The default is 1 — full monochrome — which is the second
    and last place in this catalogue where a default is not a visual no-op, for the same reason
    as `mirror`: adding this effect is the request."""
    return (f"hue=s={_number(1.0 - float(values['amount']))}",)


# --- Stylize. ---


def _compose_chroma_split(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """The chroma planes pulled apart horizontally.

    The shift is stored as a fraction of the frame width and turned into pixels *here*, against
    the export's width — the one composer that reads the geometry, and the reason it does is
    that a split of "six pixels" is a different look at 640 wide than at 1920. Storing the
    fraction makes the look survive a change of export size.
    """
    pixels = round(float(values["shift"]) * context.width)
    return (f"chromashift=cbh={pixels}:crh={-pixels}",)


def _compose_posterize(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Luma quantised to `levels` steps. 256 levels is the identity, and the default."""
    step = _number(256.0 / float(values["levels"]))
    return (f"lutyuv=y=trunc(val/{step})*{step}",)


def _compose_pixelate(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Down and back up with nearest-neighbour sampling. A block size of 1 is the identity."""
    size = _number(values["size"])
    return (
        f"scale=iw/{size}:ih/{size}:flags=neighbor",
        f"scale=iw*{size}:ih*{size}:flags=neighbor",
    )


#: The catalogue itself. Insertion order is the order a picker would list them in; it has no
#: effect at all on the chain, which is ordered by family and then by the Director.
#:
#: **Deliberately absent, and belonging to story 9.3 rather than to this slice:** slow zoom,
#: bloom/halation, edge treatment, scanline/CRT and pixel sort. Every one of them needs either a
#: branched filtergraph (`split`/`blend`) or the clip's own duration, and this chain is a single
#: comma-joined linear graph spliced into an argv that knows neither. Adding them is a change to
#: how `trim_args` is built, which is not this slice's to make.
_CATALOGUE: tuple[EffectDefinition, ...] = (
    EffectDefinition(
        effect_id="punch_in",
        family=FAMILY_GEOMETRY,
        label="Punch In",
        parameters=(
            NumberParameter("zoom", "Zoom", default=1.0, minimum=1.0, maximum=2.0),
        ),
        compose=_compose_punch_in,
    ),
    EffectDefinition(
        effect_id="handheld_shake",
        family=FAMILY_GEOMETRY,
        label="Handheld Shake",
        parameters=(
            NumberParameter("amplitude", "Amplitude", default=0.0, minimum=0.0, maximum=0.05),
            NumberParameter("frequency", "Frequency", default=2.0, minimum=0.1, maximum=10.0),
        ),
        compose=_compose_handheld_shake,
    ),
    EffectDefinition(
        effect_id="dutch_tilt",
        family=FAMILY_GEOMETRY,
        label="Dutch Tilt",
        parameters=(
            NumberParameter("angle", "Angle", default=0.0, minimum=-15.0, maximum=15.0),
        ),
        compose=_compose_dutch_tilt,
    ),
    EffectDefinition(
        effect_id="mirror",
        family=FAMILY_GEOMETRY,
        label="Mirror",
        parameters=(
            ChoiceParameter(
                "axis",
                "Axis",
                default="horizontal",
                choices=("horizontal", "vertical", "both"),
            ),
        ),
        compose=_compose_mirror,
    ),
    EffectDefinition(
        effect_id="grain",
        family=FAMILY_TEXTURE,
        label="Grain",
        parameters=(
            NumberParameter("strength", "Strength", default=0.0, minimum=0.0, maximum=60.0),
            NumberParameter(
                "seed", "Seed", default=0.0, minimum=0.0, maximum=65535.0, integer=True
            ),
        ),
        compose=_compose_grain,
    ),
    EffectDefinition(
        effect_id="vignette",
        family=FAMILY_TEXTURE,
        label="Vignette",
        parameters=(
            NumberParameter("angle", "Falloff", default=0.0, minimum=0.0, maximum=1.2),
        ),
        compose=_compose_vignette,
    ),
    EffectDefinition(
        effect_id="soft_focus",
        family=FAMILY_TEXTURE,
        label="Soft Focus",
        parameters=(
            NumberParameter("sigma", "Diffusion", default=0.0, minimum=0.0, maximum=8.0),
        ),
        compose=_compose_soft_focus,
    ),
    EffectDefinition(
        effect_id="sharpen",
        family=FAMILY_TEXTURE,
        label="Sharpen",
        parameters=(
            NumberParameter("amount", "Amount", default=0.0, minimum=-2.0, maximum=2.0),
        ),
        compose=_compose_sharpen,
    ),
    EffectDefinition(
        effect_id="banding_suppression",
        family=FAMILY_TEXTURE,
        label="Banding Suppression",
        parameters=(
            NumberParameter(
                "threshold", "Threshold", default=0.0001, minimum=0.0001, maximum=0.05
            ),
        ),
        compose=_compose_banding_suppression,
    ),
    EffectDefinition(
        effect_id="lut_look",
        family=FAMILY_GRADE,
        label="LUT Look",
        parameters=(
            LutParameter("lut", "Look"),
            ChoiceParameter(
                "interp",
                "Interpolation",
                default="tetrahedral",
                choices=("nearest", "trilinear", "tetrahedral"),
            ),
        ),
        compose=_compose_lut_look,
    ),
    EffectDefinition(
        effect_id="exposure",
        family=FAMILY_GRADE,
        label="Exposure",
        parameters=(
            NumberParameter("amount", "Amount", default=0.0, minimum=-1.0, maximum=1.0),
        ),
        compose=_compose_exposure,
    ),
    EffectDefinition(
        effect_id="contrast",
        family=FAMILY_GRADE,
        label="Contrast",
        parameters=(
            NumberParameter("amount", "Amount", default=1.0, minimum=0.0, maximum=3.0),
        ),
        compose=_compose_contrast,
    ),
    EffectDefinition(
        effect_id="saturation",
        family=FAMILY_GRADE,
        label="Saturation",
        parameters=(
            NumberParameter("amount", "Amount", default=1.0, minimum=0.0, maximum=3.0),
        ),
        compose=_compose_saturation,
    ),
    EffectDefinition(
        effect_id="temperature",
        family=FAMILY_GRADE,
        label="Temperature",
        parameters=(
            NumberParameter("amount", "Amount", default=0.0, minimum=-1.0, maximum=1.0),
        ),
        compose=_compose_temperature,
    ),
    EffectDefinition(
        effect_id="tint",
        family=FAMILY_GRADE,
        label="Tint",
        parameters=(
            NumberParameter("amount", "Amount", default=0.0, minimum=-1.0, maximum=1.0),
        ),
        compose=_compose_tint,
    ),
    EffectDefinition(
        effect_id="lift_gamma_gain",
        family=FAMILY_GRADE,
        label="Lift / Gamma / Gain",
        parameters=(
            NumberParameter("lift", "Lift", default=0.0, minimum=-1.0, maximum=1.0),
            NumberParameter("gamma", "Gamma", default=1.0, minimum=0.1, maximum=3.0),
            NumberParameter("gain", "Gain", default=0.0, minimum=-1.0, maximum=1.0),
        ),
        compose=_compose_lift_gamma_gain,
    ),
    EffectDefinition(
        effect_id="monochrome",
        family=FAMILY_GRADE,
        label="Monochrome",
        parameters=(
            NumberParameter("amount", "Amount", default=1.0, minimum=0.0, maximum=1.0),
        ),
        compose=_compose_monochrome,
    ),
    EffectDefinition(
        effect_id="chroma_split",
        family=FAMILY_STYLIZE,
        label="Chroma Split",
        parameters=(
            NumberParameter("shift", "Shift", default=0.0, minimum=-0.02, maximum=0.02),
        ),
        compose=_compose_chroma_split,
    ),
    EffectDefinition(
        effect_id="posterize",
        family=FAMILY_STYLIZE,
        label="Posterize",
        parameters=(
            NumberParameter(
                "levels", "Levels", default=256.0, minimum=2.0, maximum=256.0, integer=True
            ),
        ),
        compose=_compose_posterize,
    ),
    EffectDefinition(
        effect_id="pixelate",
        family=FAMILY_STYLIZE,
        label="Pixelate",
        parameters=(
            NumberParameter(
                "size", "Block Size", default=1.0, minimum=1.0, maximum=64.0, integer=True
            ),
        ),
        compose=_compose_pixelate,
    ),
)

#: Id -> definition. Built from the tuple above so a duplicate id is a startup failure rather
#: than an entry that silently disappears.
EFFECT_CATALOGUE: dict[str, EffectDefinition] = {}
for _definition in _CATALOGUE:
    if _definition.effect_id in EFFECT_CATALOGUE:
        raise RuntimeError(f"Two catalogue entries claim the id {_definition.effect_id!r}.")
    if _definition.family not in FAMILY_ORDER:
        raise RuntimeError(
            f"{_definition.effect_id!r} is in family {_definition.family!r}, "
            f"which is not one of {FAMILY_ORDER}."
        )
    EFFECT_CATALOGUE[_definition.effect_id] = _definition
del _definition


# ------------------------------------------------------------------------------------------
# The validator. The only thing that decides whether a value may reach a filter string.
#
# It is here rather than at the route (AD-27) because the decision is the catalogue's, and
# keeping it pure is what lets it be asserted by comparison. Slice C's 422 reports what this
# says; it does not decide it.
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedEffect:
    """One agreed effect: its id, its family, every declared parameter filled in, and whether
    the Director has it switched on. Nothing outside the catalogue survives into `values`."""

    effect_id: str
    family: str
    values: Mapping[str, Any]
    enabled: bool = True


def _validate_number(effect_id: str, parameter: NumberParameter, value: Any) -> float | int:
    # `bool` is an `int` in Python, and `True` would otherwise sail through as 1. A flag where a
    # number belongs is a client sending the wrong thing, and it is refused as one.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EffectRefusal(
            EFFECT_PARAMETER_TYPE_REFUSAL.format(
                effect=effect_id,
                parameter=parameter.name,
                expected="a whole number" if parameter.integer else "a number",
                value=value,
            )
        )
    number = float(value)
    if not math.isfinite(number):
        raise EffectRefusal(
            EFFECT_PARAMETER_TYPE_REFUSAL.format(
                effect=effect_id,
                parameter=parameter.name,
                expected="a finite number",
                value=value,
            )
        )
    if parameter.integer and number != int(number):
        raise EffectRefusal(
            EFFECT_PARAMETER_TYPE_REFUSAL.format(
                effect=effect_id,
                parameter=parameter.name,
                expected="a whole number",
                value=value,
            )
        )
    if number < parameter.minimum:
        raise EffectRefusal(
            EFFECT_PARAMETER_BELOW_REFUSAL.format(
                effect=effect_id,
                parameter=parameter.name,
                value=_number(number),
                bound=_number(parameter.minimum),
            )
        )
    if number > parameter.maximum:
        raise EffectRefusal(
            EFFECT_PARAMETER_ABOVE_REFUSAL.format(
                effect=effect_id,
                parameter=parameter.name,
                value=_number(number),
                bound=_number(parameter.maximum),
            )
        )
    return int(number) if parameter.integer else number


def _validate_choice(effect_id: str, parameter: ChoiceParameter, value: Any) -> str:
    if not isinstance(value, str) or value not in parameter.choices:
        raise EffectRefusal(
            EFFECT_PARAMETER_CHOICE_REFUSAL.format(
                effect=effect_id,
                parameter=parameter.name,
                choices=", ".join(parameter.choices),
                value=value,
            )
        )
    return value


def _validate_lut(effect_id: str, parameter: LutParameter, value: Any, known: set[str]) -> str:
    if not isinstance(value, str) or not value:
        raise EffectRefusal(EFFECT_LUT_UNNAMED_REFUSAL.format(effect=effect_id))
    if value not in known:
        raise EffectRefusal(EFFECT_LUT_UNKNOWN_REFUSAL.format(lut=value))
    return value


def validate_stack(
    stack: Iterable[Mapping[str, Any]], *, luts: Sequence[LutEntry] = ()
) -> tuple[ResolvedEffect, ...]:
    """A stack in; either every effect agreed and filled in, or `EffectRefusal` naming the
    offender.

    Every declared parameter comes back present, whether the spec carried it or not, so a
    composer never reaches for a default and never sees a key it did not declare. A parameter
    the spec omits takes the catalogue's default; a parameter the catalogue does not declare is
    a refusal rather than an ignored key, because an ignored key is how a typo becomes an effect
    that quietly does nothing.

    Disabled effects are validated exactly as enabled ones are. A stack is stored whole and a
    disabled card can be switched back on at any moment; validating only what is currently
    switched on would let a bad value sit in the manifest waiting for the moment it renders.
    """
    known_luts = {entry.lut_id for entry in luts}
    resolved: list[ResolvedEffect] = []
    for index, spec in enumerate(stack):
        if not isinstance(spec, Mapping):
            raise EffectRefusal(EFFECT_NOT_A_SPEC_REFUSAL.format(index=index, value=spec))
        effect_id = spec.get("effect")
        if not isinstance(effect_id, str) or not effect_id:
            raise EffectRefusal(EFFECT_MISSING_ID_REFUSAL.format(index=index))
        definition = EFFECT_CATALOGUE.get(effect_id)
        if definition is None:
            raise EffectRefusal(EFFECT_UNKNOWN_REFUSAL.format(effect=effect_id))

        given = spec.get("parameters", {})
        if given is None:
            given = {}
        if not isinstance(given, Mapping):
            raise EffectRefusal(
                EFFECT_PARAMETERS_NOT_A_MAP_REFUSAL.format(effect=effect_id, value=given)
            )

        enabled = spec.get("enabled", True)
        if not isinstance(enabled, bool):
            raise EffectRefusal(
                EFFECT_ENABLED_NOT_A_FLAG_REFUSAL.format(effect=effect_id, value=enabled)
            )

        declared = {parameter.name for parameter in definition.parameters}
        undeclared = sorted(set(given) - declared)
        if undeclared:
            raise EffectRefusal(
                EFFECT_UNKNOWN_PARAMETER_REFUSAL.format(
                    effect=effect_id,
                    parameter=undeclared[0],
                    declared=", ".join(sorted(declared)) or "no parameters",
                )
            )

        values: dict[str, Any] = {}
        for parameter in definition.parameters:
            if isinstance(parameter, LutParameter):
                values[parameter.name] = _validate_lut(
                    effect_id, parameter, given.get(parameter.name), known_luts
                )
            elif isinstance(parameter, ChoiceParameter):
                values[parameter.name] = _validate_choice(
                    effect_id, parameter, given.get(parameter.name, parameter.default)
                )
            else:
                values[parameter.name] = _validate_number(
                    effect_id, parameter, given.get(parameter.name, parameter.default)
                )
        resolved.append(
            ResolvedEffect(
                effect_id=effect_id,
                family=definition.family,
                values=values,
                enabled=enabled,
            )
        )
    return tuple(resolved)


# ------------------------------------------------------------------------------------------
# The chain builder.
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectStages:
    """The two groups `assembly.trim_args` splices in, and the only shape it needs to know.

    They are two groups rather than one list because the chain has two insertion points and
    they are not adjacent: `scale` sits between them, and `pad` closes the second. A caller that
    received one flat list would have to know where to cut it, and the whole point of composing
    here is that it does not.
    """

    geometry: tuple[str, ...] = ()
    treatment: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.geometry or self.treatment)


def build_effect_stages(
    stack: Iterable[Mapping[str, Any]],
    *,
    width: int,
    height: int,
    luts: Sequence[LutEntry] = (),
) -> EffectStages:
    """A stack and the export's geometry in; the ordered stages out, in their two groups.

    **Sorted by family as it reads** (AD-31). The outer loop walks `FAMILY_ORDER` and the inner
    loop walks the stack, which is a stable sort: the fixed order between families, and the
    Director's order preserved within each one. Storage order is therefore never load-bearing,
    and a stack that arrives out of order — copied between shots, hand-edited in the manifest,
    written by a client that did not know the rule — composes to exactly the chain the panel
    showed rather than to something undefined.

    An empty stack returns empty groups, and `trim_args` then builds the argv it builds today,
    stage for stage.

    LUT files are resolved *before* anything is composed, so a look whose file has gone since it
    was discovered is refused by name with nothing half-built behind it.
    """
    resolved = validate_stack(stack, luts=luts)
    entries = {entry.lut_id: entry for entry in luts}

    lut_arguments: dict[str, str] = {}
    for effect in resolved:
        if not effect.enabled:
            continue
        for parameter in EFFECT_CATALOGUE[effect.effect_id].parameters:
            if not isinstance(parameter, LutParameter):
                continue
            lut_id = effect.values[parameter.name]
            if lut_id in lut_arguments:
                continue
            entry = entries[lut_id]
            if not entry.path.is_file():
                raise EffectRefusal(
                    EFFECT_LUT_FILE_MISSING_REFUSAL.format(
                        lut=lut_id, path=entry.path.as_posix()
                    )
                )
            lut_arguments[lut_id] = lut_file_argument(entry.path, lut_id=lut_id)

    context = StageContext(width=width, height=height, lut_arguments=lut_arguments)
    geometry: list[str] = []
    treatment: list[str] = []
    for family in FAMILY_ORDER:
        target = geometry if family in PRE_SCALE_FAMILIES else treatment
        for effect in resolved:
            if not effect.enabled or effect.family != family:
                continue
            target.extend(EFFECT_CATALOGUE[effect.effect_id].compose(effect.values, context))
    return EffectStages(geometry=tuple(geometry), treatment=tuple(treatment))
