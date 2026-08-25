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

**An effect at its identity values composes to no stage at all.** Every parameter in the
catalogue but one — `mirror`'s axis, where there is no such thing — has a value that means
"leave it alone", and all of them but two *default* to it: `mirror` again, and `monochrome`,
whose default is full monochrome because adding that card is the request. The promise attached
to an identity value is that it changes no pixel, and a filter that does nothing is not free
enough to keep it: `colorbalance=rm=0:bm=0` performs no
arithmetic and still drags the frame through `yuv420p -> gbrp -> yuv420p`, which cost 47.10 dB
average PSNR measured 2026-08-25 on a 1056x608 `testsrc2`; `lutyuv` at a step of 1 leaves luma
untouched and takes chroma through 4:4:4 at u:59.81 v:63.96. So the promise is kept by *emitting
nothing*, in the composer, and the builder simply has fewer stages to splice. Not every identity
was costing a picture — `deband` at its floor and `rotate=a=0` measured `inf` — but the rule is
uniform, because "which of these no-ops is really a no-op" is not a thing a Director should have
to know.

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
import json
import math
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEBAND_FLOOR",
    "DEFAULT_LUTS",
    "DEFAULT_LUT_SIZE",
    "EFFECT_CATALOGUE",
    "EFFECT_SPEC_KEYS",
    "FAMILY_GEOMETRY",
    "FAMILY_GRADE",
    "FAMILY_ORDER",
    "FAMILY_STYLIZE",
    "FAMILY_TEXTURE",
    "FINGERPRINT_CHUNK_BYTES",
    "LUT_DIRECTORY_NAME",
    "LUT_HEADER_SCAN_BYTES",
    "LUT_SCAN_CHUNK_BYTES",
    "LUT_SUFFIX",
    "PREVIEW_FINGERPRINT_INPUTS",
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
    "preview_fingerprint",
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
EFFECT_UNKNOWN_KEY_REFUSAL = (
    "{effect} has no key called {key!r}. It takes {declared}. Nothing was composed."
)
EFFECT_STACK_NOT_A_LIST_REFUSAL = (
    "An effect stack is a list of effects, and {value!r} is not. Nothing was composed."
)
EFFECT_PARAMETER_TYPE_REFUSAL = (
    "{effect}'s {parameter} must be {expected}, and {value!r} is not. Nothing was composed."
)
EFFECT_PARAMETER_TOO_LARGE_REFUSAL = (
    "{effect}'s {parameter} is a whole number too large for this application to read as a "
    "number at all. It takes a number between {minimum} and {maximum}. Nothing was composed."
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

#: How much of a file is read at a time while its table is counted against the size its header
#: declared. The count has to reach the end of a complete table — there is no other way to tell a
#: half-copied download from a whole one — so what is bounded here is the *memory*, never the
#: file: 64 KiB at a time, and the loop stops the moment enough lines have been seen.
LUT_SCAN_CHUNK_BYTES = 1 << 16

#: `LUT_3D_SIZE N`, at the start of a line. ffmpeg skips leading whitespace and ignores every
#: line before this one, and so does this: the pattern is anchored to a line rather than to the
#: file, so the keyword inside somebody's comment is not mistaken for the header.
_LUT_SIZE_HEADER = re.compile(rb"^[ \t]*LUT_3D_SIZE[ \t]+(\d+)", re.MULTILINE)

#: `deband`'s own threshold floor, and the Banding Suppression card's default. Named here rather
#: than written twice because the composer compares against it to decide there is nothing to
#: compose, and a catalogue whose default drifted off the composer's identity would put the
#: filter back into every chain without anybody asking for it.
DEBAND_FLOOR = 0.0001

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
    """The base id for one filename: lowercase, hyphenated, alphanumerics only.

    Derived from the filename rather than stored, so a folder is the whole of the state: drop a
    file in, it is offered; take it out, it is gone. The transformation is deliberately lossy —
    two files whose names differ only in punctuation collapse to the same base — and it is
    therefore **not** by itself the id: `discover_luts` hands the bare base to a file only when
    that file is the sole holder of it, and gives every member of a collision set
    `_collision_suffix` instead.

    So the id one *file* gets depends on whether anything else in the folder collides with it,
    and cannot depend on anything more than that. The distinction matters because a manifest
    stores the id: a folder that reshuffles must turn a stale id into a refusal, never into a
    different file. See `_collision_suffix`.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "lut"


def _collision_suffix(filename: str) -> str:
    """What distinguishes one member of a collision set: eight hex characters of its own name.

    The suffix this replaced was the member's **position** in the folder's sorted listing, and a
    position is a property of the folder rather than of the file. Two files whose names differ
    only in punctuation took `my-look` and `my-look-2`; deleting the first left the survivor
    holding `my-look` — a manifest storing that id went on grading, through a different file,
    with no refusal and nothing visible anywhere.

    A digest of the filename cannot be reshuffled by a neighbour, and no member of a collision
    set holds the bare base, so the id a stack stored either resolves to the file it always meant
    or is refused by name. The one thing it does not do is survive the *arrival* of a colliding
    neighbour: an incumbent alone in its base loses the bare id when a second file collides with
    it, and its manifests are refused rather than silently retargeted. That direction is loud, so
    it is the right way round to be wrong.
    """
    return hashlib.sha256(filename.encode("utf-8")).hexdigest()[:8]


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

    **Which is exactly why each file appears whole or not at all.** These are a megabyte each at
    the shipped lattice, and a first run interrupted part-way through one — a closed lid, a
    killed process, a full disk — used to leave a truncated file that still carried its header,
    so it was still offered, and still `exists()`, so the rule above meant it was never
    regenerated: one interruption, and a look that fails at export forever. So the text goes to a
    temporary name beside the destination and is moved onto it, and a rename is the one file
    operation that cannot be observed half-done. The temporary carries this process's id, because
    two of them may be doing this at once, and it does not end in `.cube`, so an interrupted run
    leaves something discovery ignores rather than something it offers.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for lut_id, title, transform in DEFAULT_LUTS:
        destination = directory / f"{lut_id}{LUT_SUFFIX}"
        if destination.exists():
            continue
        partial = directory / f".{lut_id}{LUT_SUFFIX}.{os.getpid()}.partial"
        try:
            partial.write_text(cube_text(size, transform, title=title), encoding="utf-8")
            partial.replace(destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        written.append(destination)
    return tuple(written)


def _looks_like_a_cube(path: Path) -> bool:
    """Whether a `.cube` file carries the mandatory header **and** the table that header promises.

    A folder is a place people put things. The extension check alone would offer a half-copied
    download or a text file someone renamed, and the Director would find out at export.

    The header alone is not enough, and this is the specific failure the check exists to catch: a
    half-copied download has `LUT_3D_SIZE N` on line 1 — it is the *end* of the file that is
    missing — so a header test passes it, ffmpeg fails at export with `Error initializing
    filters`, and the "reported by name, never a crash" property this folder claims is not true.
    So the size the header declares is read, and the file is required to hold the `N**3` lines it
    just promised.

    **Counting newlines is a deliberate under-count.** The header lines are counted with the data
    lines, so this asks for `N**3` lines *of any kind* after the file's start, which a complete
    table always has (the header itself makes up the difference when a last line has no trailing
    newline) and which a table missing more than a line or two never does. A stricter count would
    have to decide what a data line is, and a sniff that drops a Director's real look because of
    a comment or a blank line is a worse failure than the one it is preventing: a look that is
    simply not offered is invisible, where a refusal at export names itself.

    Cost is bounded twice over: the read is chunked, so a large file is never held in memory, and
    it stops the moment enough lines have been seen, so the price of a valid file is the file
    itself and the price of a truncated one is whatever is left of it. Measured 2026-08-25 over
    the Director's own pack — 48 files, 44.2 MB, every one of them complete, which is the worst
    case because nothing can stop early: **221 ms cold, 23 ms warm** for the whole listing. The
    five generated defaults are 5.0 MB and 24 ms cold.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(LUT_HEADER_SCAN_BYTES)
            match = _LUT_SIZE_HEADER.search(head)
            if match is None:
                return False
            declared = int(match.group(1))
            if declared < 2:
                return False
            needed = declared**3
            counted = head.count(b"\n")
            while counted < needed:
                chunk = handle.read(LUT_SCAN_CHUNK_BYTES)
                if not chunk:
                    return False
                counted += chunk.count(b"\n")
    except OSError:
        return False
    return True


def discover_luts(
    data_root: Path | str, *, generate_defaults: bool = True
) -> tuple[LutEntry, ...]:
    """Every look the folder holds, in filename order, each with the id a client may name.

    Generation happens only when the **folder** is absent, which is the first-run case. A folder
    that exists and is empty is a Director who deleted the defaults, and regenerating them would
    be this application arguing with them. A Director's own files sit beside the generated ones
    and are indistinguishable to the chain — that is the whole design, and it is what dissolves
    the licensing question rather than managing it.

    Ordering is by lowercased filename, so a listing does not reshuffle between calls or between
    machines. It is **not** what decides an id: an id is the file's own base, or — when more than
    one file in the folder claims that base — the base plus a digest of that file's own name. A
    listing's order therefore has no effect on what any id points at, which is what makes an id
    safe to store in a manifest.
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
    files = [path for path in candidates if path.is_file() and _looks_like_a_cube(path)]
    # Two passes, because whether a file may hold the bare base id is a question about the whole
    # folder and cannot be answered while walking it.
    holders: dict[str, int] = {}
    for path in files:
        base = lut_id_for_name(path.stem)
        holders[base] = holders.get(base, 0) + 1
    entries: list[LutEntry] = []
    taken: set[str] = set()
    for path in files:
        base = lut_id_for_name(path.stem)
        lut_id = base if holders[base] == 1 else f"{base}-{_collision_suffix(path.name)}"
        suffix = 2
        while lut_id in taken:
            # Unreachable for two `.cube` files in one folder, which cannot share a name: this
            # is here so that a digest collision produces two ids rather than one entry that
            # silently disappears from the listing.
            lut_id = f"{base}-{_collision_suffix(path.name)}-{suffix}"
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

    The dimensions are the export's, never the take's, and they describe the **delivery grid** a
    treatment stage is being composed for rather than the frame it will be handed. The difference
    matters and is easy to read past: `scale=W:H:force_original_aspect_ratio=decrease` fits the
    take *inside* that grid, so a 4:3 take into a 16:9 export arrives at a treatment stage 810
    wide, not 1056 — `pad` is what makes it the export's size, and `pad` comes after every
    treatment.

    So these numbers are the right thing to compose a *look* against — `chroma_split` stores a
    fraction and turns it into pixels here, so the same stored look ships the same split at any
    delivery size — and they are the wrong thing to write into a frame's geometry. No composer
    may use them to set a size. `pixelate` is the cautionary tale: see `_compose_pixelate`.

    Geometry composers run before `scale` and therefore address the take's pixels through ffmpeg's
    own `iw`/`ih`, which is why none of them read these numbers at all.
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


def _message_number(value: float) -> str:
    """A number as a *sentence* renders it. Never filter text, and never the other way round.

    `_number` above is the filter formatter, and its six decimals are load-bearing there: they
    are what makes two float states that mean the same thing compare equal as a string. Reused in
    a refusal they import that lossiness into a claim about a number, and the sentence then
    contradicts the check that produced it — a zoom of `5e-7` read `zoom is 1, below its minimum
    of 1`, and `1e308` printed as a 309-digit integer because `.6f` never goes scientific.

    `repr` is the shortest text that round-trips to the same float, so the sentence can never
    disagree with the comparison. The trailing `.0` of a whole number is dropped: a bound of `1`
    reads as a bound and `1.0` reads as a measurement, and nothing is lost, because a `repr`
    ending in `.0` is exactly a value with nothing after the point.
    """
    return repr(float(value)).removesuffix(".0")


# --- Geometry: before `scale`, addressing the take's own pixels through `iw`/`ih`. ---


def _compose_punch_in(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """A centred crop by `zoom`, which the chain's own `scale` then brings back up.

    The zoom is bounded at 1 from below, so the crop is always *inside* the source frame and can
    never expose an undefined edge — FX-11's bound, expressed as the parameter's minimum rather
    than as a clamp buried in this function.

    A zoom of exactly 1 is a crop to the frame's own size, which is the identity: no stage.
    """
    if float(values["zoom"]) == 1.0:
        return ()
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

    At zero degrees the pair reproduces its own input — measured `inf` PSNR through the real
    chain — for the price of a real `rotate` and a real `crop`. Nothing is what that is worth.
    """
    if float(values["angle"]) == 0.0:
        return ()
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

    Amplitude zero is a window the size of the frame that never moves, whatever the frequency
    says: the identity, and no stage.
    """
    amplitude = float(values["amplitude"])
    if amplitude == 0.0:
        return ()
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

    Strength zero is grain nobody would see, and the default: no stage.
    """
    if float(values["strength"]) == 0.0:
        return ()
    strength = _number(values["strength"])
    seed = _number(values["seed"])
    return (f"noise=alls={strength}:allf=t+u:all_seed={seed}",)


def _compose_vignette(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Corner falloff computed against the picture, because this runs before `pad`. An angle of
    zero is the identity the parameter defaults to, and composes to nothing."""
    if float(values["angle"]) == 0.0:
        return ()
    return (f"vignette=angle={_number(values['angle'])}",)


def _compose_soft_focus(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """A Gaussian defocus. Sigma zero is the identity, and composes to nothing."""
    if float(values["sigma"]) == 0.0:
        return ()
    return (f"gblur=sigma={_number(values['sigma'])}",)


def _compose_sharpen(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Unsharp mask on luma. Negative amounts soften, which is why the range crosses zero, and
    zero itself is the identity: no stage."""
    if float(values["amount"]) == 0.0:
        return ()
    return (f"unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount={_number(values['amount'])}",)


def _compose_banding_suppression(
    values: Mapping[str, Any], context: StageContext
) -> tuple[str, ...]:
    """`deband` with one threshold across all four planes.

    The parameter is the filter's own threshold rather than a friendly 0..1 dial, so what the
    catalogue declares and what ffmpeg receives are the same number. Its minimum is `deband`'s
    own floor, which is measurably nothing — `inf` PSNR through the real chain — so at the floor
    there is no stage at all, and the default costs not even a filter pass.
    """
    if float(values["threshold"]) <= DEBAND_FLOOR:
        return ()
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
    """Brightness zero is the identity, and an `eq` pass is not free: no stage."""
    if float(values["amount"]) == 0.0:
        return ()
    return (f"eq=brightness={_number(values['amount'])}",)


def _compose_contrast(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Contrast 1 is the identity, and an `eq` pass is not free: no stage."""
    if float(values["amount"]) == 1.0:
        return ()
    return (f"eq=contrast={_number(values['amount'])}",)


def _compose_saturation(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Saturation 1 is the identity, and an `eq` pass is not free: no stage."""
    if float(values["amount"]) == 1.0:
        return ()
    return (f"eq=saturation={_number(values['amount'])}",)


def _compose_temperature(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Warm and cool as one axis: red up and blue down together, in the midtones only, so the
    black point does not take a cast.

    Zero composes to nothing, and this is the effect the measurement was taken on: the filter's
    zero-valued arithmetic is a no-op, but the `yuv420p -> gbrp -> yuv420p` round-trip it forces
    is not — 47.10 dB average against the same chain without it, measured 2026-08-25 over a
    1056x608 `testsrc2`. A Temperature card added and left at 0 cost the picture exactly that.
    """
    amount = float(values["amount"])
    if amount == 0.0:
        return ()
    return (f"colorbalance=rm={_number(amount)}:bm={_number(-amount)}",)


def _compose_tint(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """The axis perpendicular to temperature: green against magenta — and the same 47.10 dB at
    zero, for the same reason. No stage."""
    if float(values["amount"]) == 0.0:
        return ()
    return (f"colorbalance=gm={_number(values['amount'])}",)


def _compose_lift_gamma_gain(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Two stages, because the three controls do not live in one ffmpeg filter: shadows and
    highlights are `colorbalance`, and the midtone curve between them is `eq`'s gamma.

    All three at their identity values composes to neither stage: this one stacked a
    `colorbalance` *and* an `eq` to reproduce its own input, at the same measured 47.10 dB as
    Temperature. Any one of the three off its identity is still both stages, because the pair is
    one control and its halves are not separable.
    """
    if (
        float(values["lift"]) == 0.0
        and float(values["gain"]) == 0.0
        and float(values["gamma"]) == 1.0
    ):
        return ()
    lift = _number(values["lift"])
    gain = _number(values["gain"])
    return (
        f"colorbalance=rs={lift}:gs={lift}:bs={lift}:rh={gain}:gh={gain}:bh={gain}",
        f"eq=gamma={_number(values['gamma'])}",
    )


def _compose_monochrome(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Saturation removed by `amount`. The default is 1 — full monochrome — which is the second
    and last place in this catalogue where a *default* is not a visual no-op, for the same reason
    as `mirror`: adding this effect is the request.

    An amount of 0 is an identity *value* even though it is not the default, so it composes to
    nothing like every other identity here: `hue=s=1` reproduces its input and charges for it.
    """
    if float(values["amount"]) == 0.0:
        return ()
    return (f"hue=s={_number(1.0 - float(values['amount']))}",)


# --- Stylize. ---


def _compose_chroma_split(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """The chroma planes pulled apart horizontally.

    The shift is stored as a fraction of the frame width and turned into pixels *here*, against
    the export's width — the one composer that reads the geometry, and the reason it does is
    that a split of "six pixels" is a different look at 640 wide than at 1920. Storing the
    fraction makes the look survive a change of export size.

    The identity is tested on the *pixels* rather than on the fraction, because the pixels are
    what the filter would be given: a shift too small to move a whole one at this width is a
    shift of none, and composes to nothing rather than to `chromashift=cbh=0:crh=0`.
    """
    pixels = round(float(values["shift"]) * context.width)
    if pixels == 0:
        return ()
    return (f"chromashift=cbh={pixels}:crh={-pixels}",)


def _compose_posterize(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Luma quantised to `levels` steps. 256 levels is the identity, and the default: no stage.

    Not merely a wasted pass. `lutyuv` at a step of 1 leaves luma alone — `inf` PSNR on the Y
    plane — and takes the chroma planes through a 4:4:4 round-trip on the way there, measured
    2026-08-25 at u:59.81 v:63.96 against the same chain without it. A Posterize card sitting at
    its own default was quietly costing chroma.
    """
    levels = float(values["levels"])
    if levels >= 256.0:
        return ()
    step = _number(256.0 / levels)
    return (f"lutyuv=y=trunc(val/{step})*{step}",)


def _compose_pixelate(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Blocks, quantised in place — the frame that leaves is the size of the frame that arrived.

    It used to be a `scale` down and a `scale` back up, and that pair does not round-trip: the
    division truncates, so the multiplication cannot restore a size the block count does not
    divide. Measured on a full-frame white source: a 1056x608 export at size 64 handed `pad` a
    1024x576 frame, and `pad` centred it inside a **16-pixel black border on all four sides**,
    corner pixel `00 00 00`, around a shot with no letterbox at all. 1920x1080 at size 7 gave the
    same border 1 pixel wide. It survived review because the acceptance test exercised size 4 on
    320x240 — the one arm of the range where both divisions come out exact — and asserted only
    that ffmpeg was happy.

    Writing the original dimensions back explicitly is not open to this composer. It runs *after*
    `scale`, and `scale=W:H:force_original_aspect_ratio=decrease` produces a frame whose size
    depends on the take's own shape — `pad` is what turns it into the export's geometry
    afterwards — so the number to restore is not one anything here knows. `pixelize` needs no
    such number: it quantises in place at every block size and every frame shape.

    The mode is written out rather than left to the filter's default, so the stage text is this
    application's decision and stays put if a later ffmpeg changes its mind about the default.
    """
    size = int(values["size"])
    if size == 1:
        return ()
    return (f"pixelize=w={size}:h={size}:mode=avg",)


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
                "threshold", "Threshold", default=DEBAND_FLOOR, minimum=DEBAND_FLOOR, maximum=0.05
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


#: Every key an effect spec may carry, and the whole of the shape slice C accepts as JSON. It is
#: a tuple rather than a check written into the validator because the refusal prints it: a client
#: that misspelled one is told what the three are, in this order.
EFFECT_SPEC_KEYS: tuple[str, ...] = ("effect", "enabled", "parameters")


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
    # A Python `int` is unbounded and JSON permits the literal, so a 401-digit `zoom` genuinely
    # arrives over the wire — and `float()` answers it with `OverflowError`, which is not an
    # `EffectRefusal` and so escaped every caller's `except EffectRefusal` as a 500, on the write
    # route and again at export. It is refused here like any other unusable number. Note this is
    # *not* the non-finite case one line below: a 401-digit integer is perfectly finite, it is
    # simply wider than a double, and telling a Director it "is not a finite number" would be a
    # false sentence. The value is named by its parameter and its bounds rather than printed —
    # `repr` of an integer past 4300 digits raises in its own right, which is the very shape of
    # fault being closed.
    try:
        number = float(value)
    except OverflowError:
        raise EffectRefusal(
            EFFECT_PARAMETER_TOO_LARGE_REFUSAL.format(
                effect=effect_id,
                parameter=parameter.name,
                minimum=_message_number(parameter.minimum),
                maximum=_message_number(parameter.maximum),
            )
        ) from None
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
                value=_message_number(number),
                bound=_message_number(parameter.minimum),
            )
        )
    if number > parameter.maximum:
        raise EffectRefusal(
            EFFECT_PARAMETER_ABOVE_REFUSAL.format(
                effect=effect_id,
                parameter=parameter.name,
                value=_message_number(number),
                bound=_message_number(parameter.maximum),
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


def _validate_lut(
    effect_id: str,
    parameter: LutParameter,
    value: Any,
    known: set[str],
    *,
    enabled: bool = True,
) -> str:
    """The look a grade names, checked against the folder — unless the card is switched off.

    The two halves of that sentence divide on where the fault lives. *Is a look chosen* is a
    question about the spec, and a spec is wrong whether or not the Director has the card on, so
    it is asked either way. *Does that look still exist* is a question about the folder, and a
    disabled card is not applying a grade — refusing an export over a file a switched-off card
    names would mean one deleted `.cube` bricks every project that ever held that card.

    `build_effect_stages` has always skipped the **file**-existence check for a disabled effect.
    This is the same tolerance one function earlier, where the **id** is checked, and the two are
    now consistent rather than half of one and half of the other.
    """
    if not isinstance(value, str) or not value:
        raise EffectRefusal(EFFECT_LUT_UNNAMED_REFUSAL.format(effect=effect_id))
    if enabled and value not in known:
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

    An undeclared **key** is refused for the same reason an undeclared parameter is, and it is
    the level a client actually gets wrong: `paramters` for `parameters` would otherwise compose
    the effect at its defaults and report nothing at all.

    Disabled effects are validated exactly as enabled ones are, with one exception that is
    stated in `_validate_lut` and is about the folder rather than about the stack. A stack is
    stored whole and a disabled card can be switched back on at any moment; validating only what
    is currently switched on would let a bad value sit in the manifest waiting for the moment it
    renders.
    """
    try:
        specs = list(stack)
    except TypeError:
        # Not iterable at all. Slice C's 422 is built on this boundary raising `EffectRefusal`
        # and nothing else, so the one shape that used to leave as a `TypeError` leaves as a
        # sentence instead.
        raise EffectRefusal(EFFECT_STACK_NOT_A_LIST_REFUSAL.format(value=stack)) from None
    known_luts = {entry.lut_id for entry in luts}
    resolved: list[ResolvedEffect] = []
    for index, spec in enumerate(specs):
        if not isinstance(spec, Mapping):
            raise EffectRefusal(EFFECT_NOT_A_SPEC_REFUSAL.format(index=index, value=spec))
        effect_id = spec.get("effect")
        if not isinstance(effect_id, str) or not effect_id:
            raise EffectRefusal(EFFECT_MISSING_ID_REFUSAL.format(index=index))
        definition = EFFECT_CATALOGUE.get(effect_id)
        if definition is None:
            raise EffectRefusal(EFFECT_UNKNOWN_REFUSAL.format(effect=effect_id))

        # `key=repr` throughout: a JSON object has string keys, but this validator is also the
        # boundary for a hand-edited manifest and a Python caller, and `sorted` over keys of two
        # types raises `TypeError` — which is the one thing this function must never do.
        unknown_keys = sorted(set(spec) - set(EFFECT_SPEC_KEYS), key=repr)
        if unknown_keys:
            raise EffectRefusal(
                EFFECT_UNKNOWN_KEY_REFUSAL.format(
                    effect=effect_id,
                    key=unknown_keys[0],
                    declared=", ".join(EFFECT_SPEC_KEYS),
                )
            )

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
        undeclared = sorted(set(given) - declared, key=repr)
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
                    effect_id, parameter, given.get(parameter.name), known_luts, enabled=enabled
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


# ------------------------------------------------------------------------------------------
# The preview fingerprint (AD-23, AD-28).
#
# It lives at the *end* of this module rather than beside `song_fingerprint` because it is a
# fingerprint of the chain's inputs and it borrows the chain's own number formatter: two states
# that compose to the same filter text must fingerprint the same, and `_number` is the single
# function that decides when two floats mean one filter string. Put it above the composers and
# that relationship reads as an accident.
# ------------------------------------------------------------------------------------------


def _canonical(value: Any) -> str:
    """One value as text that depends on the value and on nothing else.

    Written out rather than delegated to `json.dumps(..., sort_keys=True)` for two reasons, both
    of which AD-28 names when it says "never an ad-hoc hash of a dict, whose ordering and float
    repr are not contracts":

    *Numbers.* A Shot's `parameters` is `dict[str, Any]` on the model — deliberately, because the
    catalogue in this module is the only thing entitled to say what a parameter is — so pydantic
    coerces nothing inside it. `{"amount": 1}` and `{"amount": 1.0}` therefore reach here as two
    different Python objects that compose to **one** filter string, and `json.dumps` would call
    them two different looks and re-render a preview that cannot possibly differ. `_number` is
    the formatter the composers use, so the fingerprint changes exactly when the chain does.

    *Types `json` refuses.* This walks a hand-edited manifest's leftovers as readily as a
    validated stack — the fingerprint is taken **before** anything decides the stack composes, so
    that an uncomposable stack still gets a name and its refusal is not a `TypeError`.

    A string is quoted and a number is not, so `"1"` and `1` cannot collide; a mapping's keys are
    ordered by `repr` for `validate_stack`'s reason, that a hand-edited manifest can mix key
    types and `sorted` over two of them raises the one exception this must never raise.
    """
    if value is None:
        return "null"
    # Before the number branch: `bool` is an `int` in Python, and `True` would otherwise
    # fingerprint identically to `1`.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, Mapping):
        pairs = sorted(value.items(), key=lambda item: repr(item[0]))
        return "{" + ",".join(f"{_canonical(k)}:{_canonical(v)}" for k, v in pairs) + "}"
    if isinstance(value, Iterable):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    return json.dumps(repr(value))


#: The eight inputs of the preview fingerprint, in the order they are hashed in. A tuple rather
#: than eight literals inside the function so that the order is a thing a test can read, and so
#: that adding a ninth is visibly a change to AD-28 rather than a line in a payload.
PREVIEW_FINGERPRINT_INPUTS: tuple[str, ...] = (
    "take",
    "window",
    "offset",
    "stack",
    "bindings",
    "song",
    "transition",
    "geometry",
)


def preview_fingerprint(
    *,
    take: str,
    window_start: float,
    window_duration: float,
    offset: float,
    stack: Iterable[Mapping[str, Any]] = (),
    bindings: Iterable[Any] = (),
    song_fingerprint: str = "",
    transition: Any = None,
    width: int,
    height: int,
) -> str:
    """The name of the Preview Clip for one state of one Shot: a SHA-256 over the eight
    inputs of `PREVIEW_FINGERPRINT_INPUTS`, in that order.

    **This is the whole of the staleness mechanism** (AD-23). Nothing stores a flag saying a
    preview is out of date: a caller recomputes this and either the file it names is on disk or
    it is not. That is why the answer is a *name* rather than a comparison — a stale entry is
    inert rather than wrong, and the cache holding a hundred obsolete clips costs disk and
    nothing else. A stored flag would be a second truth that outlives what it describes, which
    is the rule AD-21 and AD-23 both exist to keep.

    **The two empty slots are load-bearing.** `bindings` (Epic 10) and `transition` (Epic 11) do
    not exist on any model yet, and both are hashed *now*, as their empty values. Adding them
    later then changes the fingerprint only of the Shots that acquire one. Leaving them out and
    adding them later would reshape the payload for every Shot at once and invalidate every
    cached preview in every project on the day that epic merges — for looks that did not change.

    `song_fingerprint` is the **stored** content fingerprint of the song the envelope was
    measured from, not a fresh read of the file: it is here for the bindings that will be driven
    by that envelope, and re-hashing a multi-megabyte master on every drag of a slider would
    spend the entire preview budget answering a question about audio no preview yet plays.

    Floats go through `_number`, the composers' own formatter, so this changes exactly when the
    filter chain changes and never for a difference the chain cannot express — `1` and `1.0` are
    one look, and so are two zoom values a millionth apart.
    """
    fields = (
        _canonical(take),
        f"{_number(window_start)}+{_number(window_duration)}",
        _number(offset),
        _canonical(list(stack)),
        _canonical(list(bindings)),
        _canonical(song_fingerprint),
        _canonical(transition),
        f"{int(width)}x{int(height)}",
    )
    payload = "\n".join(
        f"{name}={value}"
        for name, value in zip(PREVIEW_FINGERPRINT_INPUTS, fields, strict=True)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
