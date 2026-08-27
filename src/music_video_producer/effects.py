r"""The one place a derived artefact is tied to the thing it was derived from.

AD-28 asks for *one* fingerprint function rather than an ad-hoc hash per consumer, and the
reason is that a fingerprint is only useful if every writer and every reader compute it the
same way. Two hashes that disagree do not report "stale" — they report it inconsistently,
which is worse than not checking at all.

`song_fingerprint` is where the module started and is no longer most of what it does. This
opening sentence read *"Today there is exactly one: `song_fingerprint`. The module is created
now, holding only that"* until 2026-08-26, by which point the file had passed 2,500 lines and its own
`---` divider fifteen lines below said so. The prediction that sentence made was the right one
and it came true: the later effects work put its siblings here rather than inventing a second
convention, so the module now holds the fingerprints (`song_fingerprint`, `preview_fingerprint`),
the LUT folder, the effect catalogue, the composers and the chain builder. Read the divider
below for the second half of the job; what follows immediately is still about the first.

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

---

**A stage may be a filtergraph rather than a filter** (story 9.7). `-vf` takes a whole graph
description, not only a comma-joined chain: `;` separates chains and `[label]` names a link, and
a graph is still legal for `-vf` as long as exactly one input and one output are left unlabelled.
So a composer that needs two inputs emits **one** string that happens to contain semicolons, and
`assembly.trim_args` joins it with its neighbours by a comma exactly as before, knowing nothing
about it. The shape is always the same, and `_branch_stage` is the only thing that writes it:

```
split=2[fx3a][fx3b];[fx3b]<the treated leg>[fx3c];[fx3a][fx3c]<the recombination>
```

The first chain ends on two *labelled* outputs, so the comma that follows it in the joined chain
is the separator of the last chain rather than of the first — which is why the branch has to be
written as three chains and not as a `split` whose legs are spliced in separately. The labels
carry the effect's **slot**, its position in the composed chain, because nothing forbids a stack
from holding two Blooms and two branches sharing a label name is an ffmpeg error, not a
different picture. The slot is counted over the *composed* order, so storage order stays
un-load-bearing (AD-31).

**A branch costs one frame, and the frame is bought back at the head of the chain.** Measured
2026-08-26 against this project's ffmpeg 7.0: any framesync filter — `blend`, `overlay`, every
two-input filter there is — reports end-of-file at the *last frame's* presentation timestamp
rather than one frame's duration past it, and the `fps` stage that closes every chain therefore
emits one frame fewer than it was handed. 48 frames in, 47 out, with `1 frames dropped` in
ffmpeg's own accounting, at every branch count from one to four (the loss is a property of the
graph's end, not of how many branches are in it). It is worse than a rounding: with
`setpts=PTS-STARTPTS` upstream — which every clip at a non-zero offset gets — frame durations
are zeroed, so nothing appended *after* the branch can restore the frame either, because a
cloned frame inherits the previous one's timestamp and `fps` discards it as a duplicate.

`BRANCH_FRAME_GUARD` is the answer, and it is placed **first in the whole effect chain**, ahead
of the geometry group, where frame durations are still the decoder's. It clones one frame onto
the end of the stream, `fps` drops one, and the count is exactly what it was. Verified as
content and not only as a count: a branch composed at its identity values, guard and all, is
**bit-identical** to the same chain with no effects at all — 20 of 20 frames at `inf` PSNR — so
the cloned frame never reaches the file. And it cannot over-deliver either, because `trim_args`
always closes with `-frames:v`, which caps the count from above whether or not `fps` drops
anything.

**A composer knows where its clip sits inside its Shot.** `assembly_plan` resolves a Shot with
another nested inside it into *two* `ClipWindow`s carrying one shot id, and `trim_args` restarts
the graph clock at zero for each of them. A stage that reads `t` would therefore replay itself
partway through the Shot — `handheld_shake` snapping back to phase zero, `grain` running the
same noise sequence twice. So `StageContext` carries `clip_offset`, the seconds from the Shot's
first frame to this clip's first frame, and every stage that is a function of time is a function
of `t + clip_offset` instead. The offset is always written, including the `+ 0` of a Shot that
was never split, so that the difference between two clips of one Shot is visible in the text as
exactly their difference in the timeline.

`shot_seconds` is the other half, and it is the **Shot's** whole window rather than the clip's
own length. A slow zoom given the clip's length would restart its ramp at the seam, which is the
defect this pairing exists to remove; given the Shot's, the second clip picks the ramp up where
the first one left it — **to within half a frame**. The residue is real and it is recorded:
`clip_offset` is the un-rounded timeline offset, while the picture resumes at
`round(clip.offset * 24)` frames into the take, so the clock and the frame it is applied to can
differ by up to half a frame at a seam (measured: 16.667 ms, 0.4 frame, on a real plan). It is a
sub-frame continuity error and not a count, dimension or timestamp defect — see the
`spec-9-7-audit-fixes.md` entry in `deferred-work.md` for the measurement and the repair.
Neither number is discoverable here — `effects.py` imports the standard
library and nothing else, and `assembly.py` does not import this module back — so both arrive
through the caller, from the `ClipWindow` the export is already iterating.
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
    "BRANCH_FRAME_GUARD",
    "BRANCH_LEG_FORMAT",
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
    "LOOK_PROBE_HEIGHT",
    "LOOK_PROBE_SECONDS",
    "LOOK_PROBE_WIDTH",
    "LUT_DIRECTORY_NAME",
    "LUT_HEADER_SCAN_BYTES",
    "LUT_SCAN_CHUNK_BYTES",
    "LUT_SUFFIX",
    "MAX_LUMA_CODE",
    "MIN_LUMA_CODE",
    "PREVIEW_FINGERPRINT_INPUTS",
    "PRE_PAD_FAMILIES",
    "PRE_SCALE_FAMILIES",
    "SEAM_SEED_PER_SECOND",
    "SHARPEN_MATRIX",
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
    "exported_look",
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
#: Why an effect that ramps over its Shot could not be composed. Unreachable from the
#: application — a Shot's duration is `gt=0` on the model and both callers read it from a
#: `ClipWindow` — so this is a guard on the *caller*, not a sentence a Director is expected to
#: meet: a `build_effect_stages` that was handed no span would otherwise divide by zero inside a
#: filter expression and fail in ffmpeg with a message about the wrong thing entirely.
EFFECT_NO_SPAN_REFUSAL = (
    "{effect!r} ramps over its Shot's own length, and no length was given to compose it "
    "against. Nothing was composed."
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
    """What a composer is allowed to know: the geometry being composed for, the geometry the
    stored look is written against, and the file argument for every look the stack named.

    The dimensions are never the take's, and they describe the **delivery grid** a treatment stage
    is being composed for rather than the frame it will be handed. The difference matters and is
    easy to read past: `scale=W:H:force_original_aspect_ratio=decrease` fits the take *inside*
    that grid, so a 4:3 take into a 16:9 export arrives at a treatment stage 810 wide, not
    1056 — `pad` is what makes it the export's size, and `pad` comes after every treatment.

    So these numbers are the right thing to compose a *look* against — `chroma_split` stores a
    fraction and turns it into pixels here, so the same stored look ships the same split at any
    delivery size — and they are the wrong thing to write into a frame's geometry. No composer
    may use them to set a size. `pixelate` is the cautionary tale: see `_compose_pixelate`.

    Geometry composers run before `scale` and therefore address the take's pixels through ffmpeg's
    own `iw`/`ih`, which is why none of them read these numbers at all.

    **`reference_width` is the width the stack's *pixel-denominated* parameters mean**, and it is
    the answer to a defect the fraction above only ever solved for one effect. Five parameters in
    this catalogue are a count of pixels rather than a fraction of anything — Soft Focus' sigma,
    Bloom's radius, Pixelate's and Pixel Shuffle's block, and the unsharp kernel Sharpen is
    written around — and a count of pixels covers twice as much of the frame when the frame is
    half the size. Measured 2026-08-26 through the real chain: `pixelate size=32` gave **60**
    blocks across at 1920 and **30** at 960, and `soft_focus sigma=8` spread the same edge over
    1.458 % of the frame at 1920 and **2.917 %** at 960. The preview composes at half the
    export's grid (`app.preview_side`), so five of twenty-five effects were showing a Director
    twice the look the export would ship, against a story that promises the preview differs from
    the export "in nothing else".

    Storing those five as fractions would have closed it too, and it is deliberately not what
    happened: a stored `size: 32` would then mean something new, every manifest already holding
    one would need migrating, and the **export's argv would move** — which is the one thing this
    correction may not do. So the number goes on meaning exactly what it meant, and the *chain*
    honours the grid it is composed for: `pixel_scale` is `width / reference_width`, and the five
    composers multiply by it.

    Zero — the default — means "these dimensions **are** the reference", which is every caller
    that composes at the size the look was written for: the export, `exported_look`'s probe, and
    every test that names one geometry. `pixel_scale` is then exactly `1.0`, the five composers
    multiply by nothing, and the text is character for character what it has always been.

    It is a width alone rather than a pair because the scale it expresses is uniform — the
    preview halves both axes — and because `chroma_split` already reads only the width for the
    same reason: one number cannot describe two different scales, and an anamorphic preview is
    not a thing this application makes.

    **`clip_offset` and `shot_seconds` are where this clip sits inside its Shot**, and they are
    the reason a time-dependent stage does not replay itself at a seam. See the module
    docstring: a Shot with another nested inside it becomes two clips, each of which starts the
    filter graph's clock at zero, so every stage that reads `t` reads `t + clip_offset` instead.
    `shot_seconds` is the **Shot's** whole window and never the clip's own length — a ramp
    measured against the clip would restart at the seam, which is the whole defect.

    **`slot` is this effect's position in the composed chain**, and it exists for exactly one
    purpose: naming the links of a branched stage. Nothing forbids a stack from holding two
    Blooms, and two branches sharing a label is an ffmpeg error rather than a different picture.
    It counts the *composed* order rather than the stored one, so a stack copied or hand-edited
    out of family order still composes to the same text (AD-31). No composer may read it for
    anything else; it is a name, not a number about the picture.
    """

    width: int
    height: int
    lut_arguments: Mapping[str, str] = field(default_factory=dict)
    clip_offset: float = 0.0
    shot_seconds: float = 0.0
    slot: int = 0
    reference_width: int = 0

    @property
    def pixel_scale(self) -> float:
        """What one pixel of the stored look is worth on the grid being composed for.

        Exactly `1.0` — the identity, not a float that rounds to it — whenever no reference was
        named or the reference is this very grid, which is every export. `_at_reference` and
        `_pixels_at_reference` short-circuit on that value, so a chain composed at the size its
        numbers were written for is byte-identical to the chain this module has always built.
        """
        if self.reference_width <= 0 or self.reference_width == self.width:
            return 1.0
        return self.width / self.reference_width


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


def _at_reference(value: float, context: StageContext) -> float:
    """One pixel-denominated parameter, as many pixels as it is worth on *this* grid.

    A continuous count — a blur's sigma — so nothing is rounded and nothing is floored: half of
    a sigma of 8 is a sigma of 4, and half of a sigma of 0.5 is a sigma of 0.25, which is still
    a blur. The identity value each of these composers tests is tested on the **stored** number
    rather than on this one, so which effects compose a stage is the same answer at every
    geometry — a preview that dropped a card the export runs would be the defect this closes,
    wearing different clothes.
    """
    scale = context.pixel_scale
    return float(value) if scale == 1.0 else float(value) * scale


def _pixels_at_reference(value: float, context: StageContext, *, floor: int) -> int:
    """One pixel-denominated parameter that must be a whole number of pixels, on *this* grid.

    A block size, where a half is not a thing a filter can be given. Rounded to the nearest whole
    pixel and floored at what the filter will take, and — like `_at_reference` — returning the
    stored number untouched when the grid is the one it was written for, so an export's argv is
    the argv it has always been rather than the argv a round trip through `round` produces.
    """
    scale = context.pixel_scale
    if scale == 1.0:
        return int(value)
    return max(floor, round(float(value) * scale))


def _odd_pixels_at_reference(value: int, context: StageContext, *, low: int, high: int) -> int:
    """The same, for a parameter ffmpeg insists is an **odd** number of pixels.

    `unsharp`'s matrix is the only one: it takes 3 to 23, odd, and refuses everything else. So the
    scaled width is pulled to the nearest odd number and clamped into the filter's own range,
    which is why a 5-pixel kernel at half the grid is 3 rather than 2.5 or 2.
    """
    scale = context.pixel_scale
    if scale == 1.0:
        return int(value)
    scaled = round((float(value) * scale - 1.0) / 2.0) * 2 + 1
    return max(low, min(high, scaled))


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


#: The one stage no effect asks for and every branched chain needs, placed **first** in the whole
#: chain — ahead of the geometry group, where a frame still carries the duration the decoder gave
#: it. See the module docstring for the measurement: a framesync filter reports end-of-file at the
#: last frame's timestamp rather than a frame's duration past it, so the `fps` stage that closes
#: every chain emits one frame fewer than it received, and the frame rule is broken by a graph
#: that ffmpeg reports no error about. This clones one frame onto the end; `fps` drops one; the
#: count is what it was. It is emitted only when something in the chain actually branched, so an
#: unbranched chain is the chain it has always been, stage for stage.
BRANCH_FRAME_GUARD = "tpad=stop=1:stop_mode=clone"

#: The pixel format a branch pins itself to, on the way **in** and on the way **out** of the
#: treated leg, when the leg's own filter negotiates something wider.
#:
#: **It is there for the leg that is *not* written.** `split` has one format for its input and
#: both its outputs, so a leg filter that wants `gbrp` or `yuv444p` decides the format of the
#: pristine copy too — and because the stage upstream of every treatment branch is `scale`,
#: which will output whatever it is asked for, ffmpeg satisfies that by negotiating **`scale`
#: itself** to the wide format and converting *both* outputs of `split` back afterwards:
#:
#:     [Parsed_scale_1] w:320 h:240 fmt:yuv420p -> fmt:gbrp csp:gbr range:pc
#:     [Parsed_blend_5] auto-inserting filter 'auto_scale_1' between 'Parsed_split_2' and
#:                      'Parsed_blend_5'
#:
#: So the copy the branch exists to preserve takes a 4:2:0 -> 4:4:4 -> 4:2:0 round trip, and
#: `_branch_stage`'s invariant — an opacity of zero reproduces the input exactly — is broken.
#: Measured 2026-08-26 on the real `trim_args` chain, at a dial the validator accepts and
#: `_number` renders `"0"`, against the same chain with no effect in it: `edge_treatment` came
#: back `y:43.21 u:35.67 v:33.24` and `pixel_shuffle` `y:49.40 u:36.84 v:34.02`.
#:
#: **Both ends are needed and neither alone is enough**, which is the part that is easy to get
#: wrong — measured as frame checksums against the effect-free chain, all four combinations:
#:
#: | pin | edge_treatment | pixel_shuffle |
#: |---|---|---|
#: | neither | differs | differs |
#: | the leg's end only | differs | differs |
#: | before `split` only | differs | differs |
#: | **both** | **bit-identical** | **bit-identical** |
#:
#: The one before `split` fixes a filter's *output* format, which fixes `split`'s input, which is
#: what stops `scale` being dragged wide; the one closing the leg fixes what `blend` has to agree
#: on, which is what stops the pristine output of `split` being converted for it. Take either
#: away and the round trip comes back.
#:
#: Only the branches that need it carry it. `bloom` (`lutyuv`, `gblur`) and `slow_zoom` (`scale`,
#: `overlay`) are 4:2:0-native, nothing drags the graph for them — bloom at an opacity of zero is
#: already bit-identical, measured the same way — and two conversion passes on every frame of
#: every export are not free.
#:
#: `yuv420p` rather than "whatever arrived", because it is what the chain is: `trim_args` pins
#: `format=yuv420p` as its last stage on every clip it builds, preview and export alike.
BRANCH_LEG_FORMAT = "format=yuv420p"

#: The luma code range a `lutyuv` threshold is written in. A parameter stored as 0..1 means
#: "where between black and white", and these are what black and white are on the wire — written
#: out rather than left as 0..255, because a threshold of 0 that lands at code 0 would sit below
#: every pixel in a legal-range picture and a threshold of 1 at 255 above every one of them.
MIN_LUMA_CODE = 16
MAX_LUMA_CODE = 235

#: The width of Sharpen's unsharp matrix, in pixels, at the grid the look was written for. The one
#: pixel-denominated number in this catalogue that is *not* a parameter — a Director sets the
#: amount, and this is the size of the neighbourhood the amount is applied over. Named rather than
#: inlined because it is scaled for a smaller grid like every other pixel count here, and a number
#: that moves with the geometry should not be a literal buried in a format string.
SHARPEN_MATRIX = 5

#: How a clip's offset within its Shot is turned into a seed step, for the one stage that has no
#: expression to offset. A millisecond is finer than a frame at every rate this application
#: renders, so two clips of one Shot can only land on the same seed by beginning at the same
#: moment — and two clips beginning at the same moment are one clip.
SEAM_SEED_PER_SECOND = 1000


def _branch_stage(
    context: StageContext,
    *,
    leg: str,
    join: str,
    leg_on_top: bool = False,
    pin_format: bool = False,
) -> str:
    """One stage that is a filtergraph rather than a filter: `split`, a treated leg, a rejoin.

    The only writer of a branch in this module, so the label convention lives in exactly one
    place. `leg` is the chain the copy is put through — one filter or several, comma-joined like
    any other chain — and `join` is the two-input filter that puts it back together with the
    untouched copy.

    `leg_on_top` picks which of the two the rejoin reads first, and it is not cosmetic: `blend`
    treats its **first** input as the top layer and mixes as `top*(1-opacity) + f(top, bottom)`
    for a mode, or `top*opacity + bottom*(1-opacity)` for `normal` — measured 2026-08-26. So an
    effect whose dial means "how much of the treatment" wants the untouched copy on top for a
    mode like `screen`, and the treated copy on top for `normal`, and in both cases an opacity
    of zero must reproduce the input exactly. Every composer below states which it chose.

    **That invariant is not free.** The untouched copy is only untouched if the graph never has
    to convert it, and whether it does is decided by the *other* leg's pixel format: `split`
    carries one format for its input and both of its outputs. `pin_format` is the answer, and it
    writes `BRANCH_LEG_FORMAT` at **both** ends of the branch — read that constant for the
    measurement, and for why one end alone leaves the defect exactly where it was. The two
    composers whose leg filter negotiates a wider format ask for it; the two that are
    4:2:0-native do not, and compose the text they always composed.

    The labels carry `context.slot` because two Blooms in one stack are a legal stack and two
    branches named alike are an ffmpeg error.
    """
    tag = f"fx{context.slot}"
    inputs = f"[{tag}c][{tag}a]" if leg_on_top else f"[{tag}a][{tag}c]"
    head = f"{BRANCH_LEG_FORMAT}," if pin_format else ""
    tail = f",{BRANCH_LEG_FORMAT}" if pin_format else ""
    return f"{head}split=2[{tag}a][{tag}b];[{tag}b]{leg}{tail}[{tag}c];{inputs}{join}"


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


def _compose_slow_zoom(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    r"""A push in or out that takes the Shot's whole length to arrive — the one geometry stage
    that is a function of time *and* of how long the Shot is.

    **Why it is a branch.** A zoom is a crop whose window changes size, and `crop` cannot do
    that: its `w` and `h` are evaluated once when the link is configured, and this build has no
    `eval` option on `crop` at all (`Option not found`, measured). `zoompan` is the filter for
    the job and cannot be used here either — its output size is an `image_size`, not an
    expression, so it would have to be told a number, and a geometry stage runs before `scale`
    and does not know the take's shape. What it *can* do is scale by an expression: `scale` takes
    `eval=frame` and reads `t`. So the frame is split, one copy is scaled up by the ramp, and
    `overlay` puts it back over the untouched copy — and `overlay`'s output is the size of its
    **main** input, which is the untouched copy, so the frame that leaves is the size of the
    frame that arrived on every frame of the clip. The scaled copy is always the larger of the
    two and is centred, so it covers the frame completely and the untouched copy underneath is
    never seen; it is there to hold the geometry, not to be looked at.

    The scale factor is never below 1, in either direction, which is FX-11's bound expressed as
    arithmetic: a zoom that sampled below 1 would show the frame's own edge. "In" ramps 1 →
    `zoom`, "out" starts at `zoom` and returns to 1, and both are clamped at the end of the
    Shot rather than allowed to run past it.

    Both dimensions are rounded down to an even number, because `yuv420p` has half-resolution
    chroma planes and an odd intermediate size is a shape ffmpeg has to guess at.

    A zoom of exactly 1 is no movement at all: the identity, and no stage.
    """
    zoom = float(values["zoom"])
    if zoom == 1.0:
        return ()
    # The guard is on the **text**, not on the value, because the text is the denominator. A span
    # under 5e-7 is a positive float that `_number` renders `"0"`, and the expression then reads
    # `min((t+0)/0\,1)` — which ffmpeg does not refuse: measured 2026-08-26 at 1e-9, 4.9e-7 and
    # 5e-7, all three rendered rc=0, 24 frames, correct dimensions, contrary to what this
    # refusal's own sentence predicts. `Shot.duration` is only `Field(gt=0)` and the preview route
    # hands it through with no window check, so a span that small is reachable; the export refuses
    # a sub-frame window before it gets here. Both halves are kept: the value catches zero and
    # every negative, the text catches the positive values that compose as zero.
    span = _number(context.shot_seconds)
    if context.shot_seconds <= 0.0 or span == "0":
        raise EffectRefusal(EFFECT_NO_SPAN_REFUSAL.format(effect="slow_zoom"))
    # `t` restarts at zero for every clip of a Shot; the offset is what makes the ramp of a Shot
    # that became two clips one ramp rather than two. Always written, `+ 0` included, so the
    # difference between two clips is legible in the text as their difference on the timeline.
    progress = rf"min((t+{_number(context.clip_offset)})/{span}\,1)"
    reach = _number(zoom - 1.0)
    if values["direction"] == "out":
        factor = f"({_number(zoom)}-{reach}*{progress})"
    else:
        factor = f"(1+{reach}*{progress})"
    leg = f"scale=w=trunc(iw*{factor}/2)*2:h=trunc(ih*{factor}/2)*2:eval=frame"
    return (
        _branch_stage(
            context, leg=leg, join="overlay=x=(W-w)/2:y=(H-h)/2:eval=frame"
        ),
    )


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

    **The clock is the Shot's, not the clip's.** `trim_args` prepends `setpts=PTS-STARTPTS` to
    any clip cut at an offset, so `t` is zero at the start of every clip — and a Shot with
    another nested inside it *is* two clips. Reading `t` alone, the shake snapped back to phase
    zero partway through the Shot, on a frame the Director never asked to be a cut. Reading
    `t + clip_offset` it does not, and the offset is written even when it is zero so that the
    two clips of one Shot differ in this text by exactly their difference on the timeline.

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
    clock = f"(t+{_number(context.clip_offset)})"
    stage = (
        f"crop=w=iw*{window}:h=ih*{window}"
        f":x=(iw-ow)/2+iw*{swing}*sin(2*PI*{frequency}*{clock})"
        f":y=(ih-oh)/2+ih*{swing}*cos(2*PI*{vertical_frequency}*{clock})"
    )
    return (stage,)


# --- Texture: after `scale`, before `pad`. The frame is already the export's size here. ---


def _compose_grain(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Temporal grain with a pinned seed.

    The seed is a parameter rather than a constant because two shots carrying the same grain
    should be allowed to differ — but it is *always written*, because `noise` without one is
    seeded from the clock and the same manifest would render a different file every time. That
    would break the standing rule that a render input is a pure function of the manifest.

    **The seed carries the clip's place in its Shot**, and it has to, because `noise` offers no
    other handle on time. Its `t` flag makes the pattern move frame to frame, but the sequence of
    patterns is a function of the seed and of how many frames have gone by since the filter
    started — and `trim_args` starts a new filter graph for every clip. A Shot that became two
    clips therefore ran the *same* noise twice, which is the one thing grain must never do,
    because identical grain over a cut is the frame the eye lands on. There is no expression to
    offset, so the offset moves the seed instead: milliseconds into the Shot, which is finer than
    a frame at any rate this application renders, so two clips of one Shot cannot collide unless
    they begin at the same moment — in which case they are one clip.

    A clip that starts at the beginning of its Shot adds nothing, so an unsplit Shot's stage is
    the stage it has always been, character for character.

    Strength zero is grain nobody would see, and the default: no stage.
    """
    if float(values["strength"]) == 0.0:
        return ()
    strength = _number(values["strength"])
    seed = _number(float(values["seed"]) + round(context.clip_offset * SEAM_SEED_PER_SECOND))
    return (f"noise=alls={strength}:allf=t+u:all_seed={seed}",)


def _compose_vignette(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Corner falloff computed against the picture, because this runs before `pad`. An angle of
    zero is the identity the parameter defaults to, and composes to nothing."""
    if float(values["angle"]) == 0.0:
        return ()
    return (f"vignette=angle={_number(values['angle'])}",)


def _compose_soft_focus(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """A Gaussian defocus. Sigma zero is the identity, and composes to nothing.

    **The sigma is a count of pixels**, so it is worth half as much of the frame on a grid half
    the size and it is scaled to the grid being composed for (`StageContext.reference_width`).
    Measured through the real chain: `sigma=8` spread a step edge over 1.458 % of the frame at
    1920 and 2.917 % at 960 before this, and 1.458 % at both after.
    """
    if float(values["sigma"]) == 0.0:
        return ()
    return (f"gblur=sigma={_number(_at_reference(values['sigma'], context))}",)


def _compose_sharpen(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    """Unsharp mask on luma. Negative amounts soften, which is why the range crosses zero, and
    zero itself is the identity: no stage.

    **The pixel-denominated number here is the matrix, not the parameter.** `amount` is a
    strength and means the same at any size; the 5x5 kernel is the thing measured in pixels, and
    the halo it rings a step edge with was 4 pixels wide at every grid — 0.208 % of the frame at
    1920 and 0.417 % at 960. So it is the matrix that scales, and it scales the way `unsharp`
    will take it: odd, and never outside the filter's own 3..23.
    """
    if float(values["amount"]) == 0.0:
        return ()
    matrix = _odd_pixels_at_reference(SHARPEN_MATRIX, context, low=3, high=23)
    return (
        (
            f"unsharp=luma_msize_x={matrix}:luma_msize_y={matrix}"
            f":luma_amount={_number(values['amount'])}"
        ),
    )


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


def _compose_bloom(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    r"""Halation: the picture's own highlights, blurred wide and screened back over it.

    **Why it is a branch.** A glow is the frame combined with a *changed copy of itself*, and no
    single filter takes two versions of one input. So the frame is split, one copy is reduced to
    its highlights and blurred, and `blend` screens the two back together.

    The highlight pass is a `lutyuv` that keeps luma above the threshold and takes everything
    below it to **zero**, and the chroma planes to zero with it. Zero is not black here, it is
    `screen`'s identity: `screen(a, 0) = a` on every plane, so a bloom leg that found no
    highlight leaves the picture untouched rather than lifting its blacks or casting its colour.
    Measured: a threshold above every pixel in the frame renders `inf` PSNR against the same
    chain with no Bloom in it, on all three planes. Taking the floor to legal black (16) instead
    lifts every shadow in the frame by up to 16 codes, which is a bloom nobody asked for.

    The untouched copy is the **top** input, so `intensity` mixes from the picture toward the
    bloomed picture and an intensity of zero is exactly the picture. Zero is the default and
    composes to no stage at all.

    **The radius is a count of pixels** and is scaled to the grid being composed for, exactly as
    Soft Focus' sigma is and for the same reason: measured through the real chain, `radius=40`
    bled its glow 3.958 % of the way across the frame at 1920 and 7.917 % at 960 before this.
    The threshold is not scaled — it is a luma code, and a highlight is the same brightness
    however large the frame is. Neither is `intensity`, which decides whether there is a stage at
    all, so this branch is emitted at exactly the same geometries it always was and the frame
    guard cannot move.
    """
    intensity = float(values["intensity"])
    if intensity == 0.0:
        return ()
    threshold = _number(
        round(MIN_LUMA_CODE + float(values["threshold"]) * (MAX_LUMA_CODE - MIN_LUMA_CODE))
    )
    leg = (
        rf"lutyuv=y=if(gt(val\,{threshold})\,val\,0):u=0:v=0,"
        f"gblur=sigma={_number(_at_reference(values['radius'], context))}"
    )
    return (
        _branch_stage(
            context,
            leg=leg,
            join=f"blend=all_mode=screen:all_opacity={_number(intensity)}",
        ),
    )


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

    **The block is a count of pixels**, which is the whole of the finding this scaling closes:
    measured through the real chain, `size=32` laid **60** blocks across the frame at 1920 and
    **30** at 960, so a Director judging a mosaic in the Monitor was judging one twice as coarse
    as the export would ship. The stored number goes on meaning what it means — the export's argv
    does not move — and the block is scaled to the grid being composed for instead.

    The identity is tested on the **composed** pixels rather than on the stored ones, which is
    `chroma_split`'s rule applied to the other pixel-denominated parameter: a block that has been
    scaled down to a single pixel quantises nothing, and a `pixelize=w=1:h=1` that changes no
    pixel is exactly the no-op this module refuses to emit.
    """
    size = _pixels_at_reference(values["size"], context, floor=1)
    if size <= 1:
        return ()
    return (f"pixelize=w={size}:h={size}:mode=avg",)


def _compose_edge_treatment(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    r"""Canny edges mixed back into the picture, at a strength the Director sets.

    **Why it is a branch.** `edgedetect=mode=colormix` already mixes its edges into the frame,
    but it mixes them all the way: the only dials it offers are the two thresholds, which decide
    *which* edges are found rather than how much of them is seen, so an edge look was either
    fully on or absent. Splitting the frame and crossfading the detected version back over the
    original turns that into a dial, and the two thresholds go on meaning what they mean.

    The thresholds are ffmpeg's own numbers rather than a friendly pair, the same decision
    Banding Suppression made: what the catalogue declares and what the filter receives are one
    number, and a Director reading the filter's documentation is reading about this control.

    The **treated** copy is on top here, because `blend`'s `normal` mode is
    `top*opacity + bottom*(1-opacity)` — measured 2026-08-26 — so with the edges on top an
    opacity of `strength` runs from the untouched picture at 0 to the full edge pass at 1. Zero
    is the default, and composes to nothing.

    `edgedetect=mode=colormix` negotiates `gbrp`, which is a format the rest of the chain does
    not use, so the branch is pinned — read `BRANCH_LEG_FORMAT` for why that is a fact about the
    *untouched* copy rather than about the edges.
    """
    strength = float(values["strength"])
    if strength == 0.0:
        return ()
    leg = (
        f"edgedetect=low={_number(values['low'])}:high={_number(values['high'])}:mode=colormix"
    )
    return (
        _branch_stage(
            context,
            leg=leg,
            join=f"blend=all_mode=normal:all_opacity={_number(strength)}",
            leg_on_top=True,
            pin_format=True,
        ),
    )


def _compose_scanlines(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    r"""A CRT's horizontal line structure, drawn as a grid with no vertical lines in it.

    The one effect here that is a picture of a *display* rather than of a lens, and the cheapest
    of the five: `drawgrid` writes translucent black rows straight into the frame, with none of
    the per-pixel arithmetic a `geq` scanline would cost on every frame of every export.

    **`drawgrid` always draws a vertical line too**, and the trick is where it is put. The grid's
    columns land at `x = x0 + k*w`, so a cell width of `iw` puts one down the very first column
    of the picture — measured on a white frame, column 0 at 126 against 255 everywhere else. A
    cell twice the frame's width is what moves every column but the first one past the right
    edge, and the origin is what has to move the first one past the left edge.

    **The origin is a whole thickness out, not one pixel out**, because a grid line spans
    `[x, x+t-1]`: thickness extends *forward* from `x`, so `x=-1` leaves columns `0 .. t-2`
    inside the picture and only `t=1` hides them. That is a real delivery's ordinary case, not
    a corner — measured on a white frame, sampling a row no scanline is on (2026-08-26):

    | height | lines | thickness | dark columns at `x=-1` | at `x=-t` |
    |---|---|---|---|---|
    | 1080 | 200 (the default) | 2 | 1 | 0 |
    | 1080 | 100 | 5 | 4 | 0 |
    | 1080 | 40 | 13 | 12 | 0 |
    | 1080 | 20 | 27 | **26** | 0 |
    | 720 | 200 | 1 | 0 | 0 |

    So the origin is `-t`, which puts the line's whole span at `-t .. -1` and nothing vertical
    survives at any height. The rows are unaffected either way, which is the other half of what
    was measured: at `x=-t` every column of a non-scanline row reads 255 and the scanline rows
    still read 126.

    The spacing is a fraction of `ih` rather than a count of pixels, so a look survives a change
    of export size — the same argument Chroma Split makes for storing its shift as a fraction —
    and it is read off the frame the stage is actually handed rather than off the delivery grid,
    which is `pad`'s business and comes later. Thickness is half the spacing, so the duty cycle
    is the same at every size, and both are floored at what a picture can represent: a cell of
    two rows and a line of one, which is a scanline every other row and the tightest a CRT look
    can honestly get.

    Strength zero is a grid drawn in nothing at all: the default, and no stage.
    """
    strength = float(values["strength"])
    if strength == 0.0:
        return ()
    spacing = rf"max(2\,trunc(ih/{int(values['lines'])}))"
    thickness = rf"max(1\,trunc(ih/{int(values['lines'])}/2))"
    return (
        (
            f"drawgrid=x=-{thickness}:y=0:w=iw*2:h={spacing}:t={thickness}"
            f":c=black@{_number(strength)}"
        ),
    )


def _compose_pixel_shuffle(values: Mapping[str, Any], context: StageContext) -> tuple[str, ...]:
    r"""Blocks of the frame swapped with each other, mixed back over the frame they came from.

    **Why it is a branch.** `shufflepixels` is total: it permutes every block in the frame and
    offers no strength, so on its own it is a scramble rather than a look. Crossfading the
    shuffled copy back over the original is what makes it a dial, from an untouched picture at 0
    to a full datamosh at 1, with the interesting range in between.

    The seed is **always written**, for the same reason Grain's is: `shufflepixels` seeds itself
    from the clock when it is not told, and a manifest that renders a different file every time
    is not a render input. Unlike Grain's it carries no clip offset, and that is not an
    oversight — the permutation is spatial and identical on every frame, so a Shot that became
    two clips shows the same arrangement across the seam, which is continuity rather than a
    repeat.

    The **shuffled** copy is on top, so `amount` runs from the picture at 0 to the shuffle at 1
    under `normal`'s `top*opacity + bottom*(1-opacity)`. Zero is the default and no stage.

    `shufflepixels` negotiates `yuv444p`, which is a format the rest of the chain does not use,
    so the branch is pinned — read `BRANCH_LEG_FORMAT` for why that is a fact about the
    *untouched* copy rather than about the shuffled one.

    **The block is a count of pixels** and is scaled to the grid being composed for, as
    `pixelate`'s is: measured through the real chain, `block=32` shuffled in granules covering
    1.667 % of the frame at 1920 and 3.333 % at 960. Its floor here is one pixel rather than the
    catalogue's minimum of two, because the floor is a fact about what the *filter* will take and
    the minimum is a fact about what a Director may store. `amount` is what decides whether there
    is a branch at all and it is not scaled, so the frame guard cannot move.
    """
    amount = float(values["amount"])
    if amount == 0.0:
        return ()
    block = _pixels_at_reference(values["block"], context, floor=1)
    leg = (
        f"shufflepixels=mode=block:width={block}:height={block}"
        f":seed={_number(values['seed'])}"
    )
    return (
        _branch_stage(
            context,
            leg=leg,
            join=f"blend=all_mode=normal:all_opacity={_number(amount)}",
            leg_on_top=True,
            pin_format=True,
        ),
    )


#: The catalogue itself. Insertion order is the order a picker would list them in; it has no
#: effect at all on the chain, which is ordered by family and then by the Director.
#:
#: **The five that used to be missing are here** (story 9.7): Slow Zoom, Bloom, Edge Treatment,
#: Scanlines and Pixel Shuffle. Every one of them needed either a branched filtergraph or the
#: clip's place inside its Shot, and until the chain could be handed both they could not be
#: written at all. Four of the five are branches; Scanlines is the one that is not, and it is
#: here with them because `drawgrid` turned out to do honestly and cheaply what the other four
#: need two inputs for. FX-9, FX-10 and FX-11's stated minimums are complete at this line.
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
        effect_id="slow_zoom",
        family=FAMILY_GEOMETRY,
        label="Slow Zoom",
        parameters=(
            NumberParameter("zoom", "Zoom", default=1.0, minimum=1.0, maximum=2.0),
            ChoiceParameter("direction", "Direction", default="in", choices=("in", "out")),
        ),
        compose=_compose_slow_zoom,
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
        effect_id="bloom",
        family=FAMILY_TEXTURE,
        label="Bloom",
        parameters=(
            NumberParameter("intensity", "Intensity", default=0.0, minimum=0.0, maximum=1.0),
            NumberParameter("threshold", "Threshold", default=0.7, minimum=0.0, maximum=1.0),
            NumberParameter("radius", "Radius", default=8.0, minimum=0.5, maximum=40.0),
        ),
        compose=_compose_bloom,
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
    EffectDefinition(
        effect_id="edge_treatment",
        family=FAMILY_STYLIZE,
        label="Edge Treatment",
        parameters=(
            NumberParameter("strength", "Strength", default=0.0, minimum=0.0, maximum=1.0),
            NumberParameter("low", "Low Threshold", default=0.08, minimum=0.0, maximum=1.0),
            NumberParameter("high", "High Threshold", default=0.2, minimum=0.0, maximum=1.0),
        ),
        compose=_compose_edge_treatment,
    ),
    EffectDefinition(
        effect_id="scanlines",
        family=FAMILY_STYLIZE,
        label="Scanlines",
        parameters=(
            NumberParameter("strength", "Strength", default=0.0, minimum=0.0, maximum=1.0),
            NumberParameter(
                "lines", "Lines", default=200.0, minimum=20.0, maximum=600.0, integer=True
            ),
        ),
        compose=_compose_scanlines,
    ),
    EffectDefinition(
        effect_id="pixel_shuffle",
        family=FAMILY_STYLIZE,
        label="Pixel Shuffle",
        parameters=(
            NumberParameter("amount", "Amount", default=0.0, minimum=0.0, maximum=1.0),
            NumberParameter(
                "block", "Block Size", default=8.0, minimum=2.0, maximum=64.0, integer=True
            ),
            NumberParameter(
                "seed", "Seed", default=0.0, minimum=0.0, maximum=65535.0, integer=True
            ),
        ),
        compose=_compose_pixel_shuffle,
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


def _is_a_filtergraph(stage: str) -> bool:
    """Whether one composed stage is a graph — several chains — rather than a single chain.

    ffmpeg's quoting, read the way ffmpeg reads it: a single-quoted run is literal to its closing
    quote, a backslash outside one escapes the next character, and a `;` that is neither is what
    separates one chain from the next. Written out as a scan rather than as `";" in stage`
    because the only client-influenced text this module can emit — a look's filename, quoted by
    `lut_file_argument` — may contain a semicolon, and `EffectStages.branched` says why that
    mattered.
    """
    quoted = False
    characters = iter(stage)
    for character in characters:
        if quoted:
            quoted = character != "'"
        elif character == "'":
            quoted = True
        elif character == "\\":
            next(characters, "")
        elif character == ";":
            return True
    return False


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

    @property
    def branched(self) -> bool:
        r"""Whether any stage here is a filtergraph rather than a filter.

        A semicolon is the whole test — but a *bare* one, which is not the same as a semicolon
        anywhere in the text, and the difference is reachable. The old test said "no filter
        option this catalogue writes contains one"; `lut_file_argument` contradicts that in its
        own docstring, where surviving "spaces, commas, semicolons, brackets" is the point of the
        quoting. A look the Director dropped in the folder as `warm;cool.cube` composes
        `lut3d=file='C\:/looks/warm;cool.cube':interp=tetrahedral`, which held a `;` with no
        `split=` anywhere in it — and this predicate is the one thing that decides whether
        `BRANCH_FRAME_GUARD` is emitted, so a linear chain was getting the frame guard.

        Measured 2026-08-26: the picture is unharmed (`tpad` on a linear chain is a no-op by
        `framemd5`) and the count is unharmed while the take covers its window, which is what the
        export's overrun check guarantees — but a 12-frame source asked for 24 renders 12 frames
        without the guard and **13** with it. Latent on the export path, not on every path, and
        load-bearing for the one thing that keeps the frame rule.

        So the scan is ffmpeg's own grammar rather than a substring: inside single quotes
        everything is literal — which is exactly why `lut_file_argument` refuses an apostrophe,
        since ffmpeg offers no escape that reaches the file inside them — and outside them a
        backslash escapes whatever follows, which is the `\,` every expression in this catalogue
        is written with. A `;` that survives both is a chain separator by definition, and
        `_branch_stage` is the only thing here that writes one.
        """
        return any(_is_a_filtergraph(stage) for stage in (*self.geometry, *self.treatment))


def build_effect_stages(
    stack: Iterable[Mapping[str, Any]],
    *,
    width: int,
    height: int,
    luts: Sequence[LutEntry] = (),
    clip_offset: float = 0.0,
    shot_seconds: float = 0.0,
    reference_width: int = 0,
) -> EffectStages:
    """A stack, the export's geometry and the clip's place in its Shot in; the stages out.

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

    **`clip_offset` and `shot_seconds` describe the clip, not the effect.** They default to a
    clip that begins where its Shot begins and a Shot whose length nothing said, which is what
    every caller that composes a whole Shot at once wants — the preview, and every clip of a
    Shot no later Shot nests inside. The one thing the defaults cannot do is compose Slow Zoom,
    which refuses by name rather than dividing by a length nobody gave it.

    **`reference_width` is for a caller composing at a size the stack was not written for**, and
    the preview is the only one there is: it renders at half the delivery grid, and the five
    pixel-denominated parameters in this catalogue would otherwise cover twice as much of its
    frame as the export will ship. Passed the export's width, the five scale and everything else
    composes exactly as it always did. Left at its default, nothing scales at all — which is what
    every export, every look record and every test naming one geometry gets, so the argv this
    module has always built is the argv it still builds. See `StageContext.reference_width`.

    **The branch guard is prepended here rather than by a composer** (see the module docstring).
    It is a property of the chain and not of any effect in it: one framesync filter anywhere
    costs the chain one frame at its `fps` stage, and four cost it the same one. So it is
    decided once, over the finished groups, and it goes at the head of the geometry group —
    which is the head of the whole chain, the last point at which a frame still carries the
    duration the decoder gave it. A chain with no branch in it never sees it.
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

    geometry: list[str] = []
    treatment: list[str] = []
    slot = 0
    for family in FAMILY_ORDER:
        target = geometry if family in PRE_SCALE_FAMILIES else treatment
        for effect in resolved:
            if not effect.enabled or effect.family != family:
                continue
            # One context per effect, and the only thing that differs between them is the slot:
            # its position in *this* order, which is what a branch's link labels are named from.
            # Counted over the composed order rather than the stored one, so a stack that was
            # copied or hand-edited out of family order composes to the same text (AD-31).
            context = StageContext(
                width=width,
                height=height,
                lut_arguments=lut_arguments,
                clip_offset=clip_offset,
                shot_seconds=shot_seconds,
                slot=slot,
                reference_width=reference_width,
            )
            slot += 1
            target.extend(EFFECT_CATALOGUE[effect.effect_id].compose(effect.values, context))
    stages = EffectStages(geometry=tuple(geometry), treatment=tuple(treatment))
    if not stages.branched:
        return stages
    return EffectStages(
        geometry=(BRANCH_FRAME_GUARD, *stages.geometry), treatment=stages.treatment
    )


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
    validated value, and it has to: `bindings` and `transition` are hashed as they arrive, so a
    `set` or a `date` that reached one of them must produce a name rather than a `TypeError`
    raised from inside a fingerprint. (This paragraph used to say the fingerprint is taken
    "before anything decides the stack composes". That was never true of its only caller, which
    composes the chain and refuses on it two dozen lines earlier, and it is not true of
    `preview_fingerprint` at all any more: the stack reaches the payload *as the chain it
    composes to*, so an uncomposable stack is a refusal rather than a name.)

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


#: The delivery `exported_look` decides "composed nothing" against when its caller does not name
#: one, and the span it probes `slow_zoom` with.
#:
#: Only one composer's identity depends on either: `chroma_split` stores its shift as a fraction
#: and turns it into pixels against the width, so a shift too small to move a whole pixel at this
#: width composes nothing. Probing at a delivery larger than anything this application produces
#: makes the default answer the conservative one — an effect is dropped from the record only if
#: it composes nothing at **every** size — so the record can name something that composed no
#: stage at the real width, and can never lose something that did. A caller with the export's own
#: geometry should pass it and get the exact answer; `app._compose_effect_chains` has it, one
#: line above the call it already makes to `build_effect_stages`.
#:
#: The span exists because `slow_zoom` refuses a Shot with no length rather than dividing by it,
#: and a record is not the place to raise. Any positive value answers the identity question,
#: which `slow_zoom` decides on `zoom` alone and before the span is looked at.
LOOK_PROBE_WIDTH = 7680
LOOK_PROBE_HEIGHT = 4320
LOOK_PROBE_SECONDS = 1.0


def exported_look(
    stack: Iterable[Mapping[str, Any]],
    *,
    luts: Sequence[LutEntry] = (),
    width: int = LOOK_PROBE_WIDTH,
    height: int = LOOK_PROBE_HEIGHT,
) -> tuple[str, ...]:
    """One stack as the record of what an export applied: `"<effect>:{canonical values}"`, in
    chain order, for the effects that actually composed a stage.

    FX-25's half of the provenance. `RenderJob.inputs` records the takes an assembly consumed;
    this records what was done to them, and it lives here rather than in the route because the
    catalogue is the only thing entitled to say what an effect's parameters are (AD-27). The
    values are `validate_stack`'s **resolved** ones — every declared parameter filled in, the
    catalogue's defaults included — so the record answers what the export was built with even for
    a parameter the Director never touched, and stays readable after the Shot's stack has moved
    on or the catalogue's defaults have changed.

    Ordered by `FAMILY_ORDER` then by the Director's own order within a family, which is
    `build_effect_stages`' order and therefore the order the filter chain ran in. Storage order is
    not load-bearing (AD-31), so recording it would make two records of one look differ over
    nothing.

    Disabled entries are omitted: they compose no stage, and a record naming one would describe a
    picture the export did not produce. `Shot.effects` keeps them, which is where the question
    "what did the Director configure" is answered.

    **So is an entry that is enabled and composes nothing anyway**, which is the same rule and
    was the same sentence, applied to the other way of reaching zero stages. Every effect in this
    catalogue has a value that means "leave it alone" and composes to no filter at all, and since
    story 9.7 all five of the newest ones **default** to it — so a Director who adds a Bloom card
    and leaves it alone was putting `bloom:{"intensity":0,...}` in the job record of an export
    that never ran a bloom. The test is the composer's own answer rather than a list of identity
    values kept here, because the catalogue is the only thing entitled to say what an effect's
    identity is, and a second copy of that would be a second truth.

    `_canonical` is the formatter, so this is the same text the preview fingerprint hashes and two
    parameter states that compose to one filter string record as one look — `{"zoom": 1}` and
    `{"zoom": 1.0}` are not two different exports.

    Refuses exactly as `validate_stack` does, for a stack that does not compose. Every caller
    reaches this only after the same stack has been agreed, so the refusal is unreachable there;
    it is not caught and re-worded here because a second wording of an existing refusal is the one
    thing this surface may not grow.
    """
    resolved = validate_stack(stack, luts=luts)
    # The look's file argument is never read: `lut_look` composes a stage at every value it has,
    # so the placeholder cannot change the answer, and a record must not touch the disk to say
    # what an export recorded.
    context = StageContext(
        width=width,
        height=height,
        lut_arguments={entry.lut_id: "" for entry in luts},
        shot_seconds=LOOK_PROBE_SECONDS,
    )
    ordered = [
        effect
        for family in FAMILY_ORDER
        for effect in resolved
        if effect.enabled
        and effect.family == family
        and EFFECT_CATALOGUE[effect.effect_id].compose(effect.values, context)
    ]
    return tuple(f"{effect.effect_id}:{_canonical(effect.values)}" for effect in ordered)


#: The eight inputs of the preview fingerprint, in the order they are hashed in. A tuple rather
#: than eight literals inside the function so that the order is a thing a test can read, and so
#: that adding a ninth is visibly a change to AD-28 rather than a line in a payload.
#:
#: The fourth was called `stack` until 2026-08-26 and hashed the stored spec. It is `chain`
#: because that is what it holds: the filter text the stack composes to. See
#: `preview_fingerprint`.
PREVIEW_FINGERPRINT_INPUTS: tuple[str, ...] = (
    "take",
    "window",
    "offset",
    "chain",
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
    luts: Sequence[LutEntry] = (),
    bindings: Iterable[Any] = (),
    song_fingerprint: str = "",
    transition: Any = None,
    width: int,
    height: int,
    reference_width: int = 0,
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

    **The stack is hashed as the chain it composes to, not as the spec it is stored as** — the
    fourth slot is `chain` for that reason. The stored stack is sparse by design
    (`models.stored_effect_stack`: *"a manifest full of frozen defaults makes a corrected default
    unable to reach the projects that would benefit from it"*), so a name taken from the sparse
    spec cannot move when the catalogue supplies a different default, and cannot move when a
    composer is corrected either. Both were live: `e4aec46` moved Scanlines' grid origin from
    `x=-1` to `x=-t`, removing a black left-edge bar measured at 26 dark columns at 1920x1080
    with `lines=20`, and every clip already cached under the old spelling went on being served —
    nothing evicts `previews/`, so it was permanent. Composing first costs no disk read the
    caller has not already made and no arithmetic worth naming, and it makes the name a function
    of the picture rather than of the words for it.

    **It costs one re-render of everything, once.** The payload changed, so every clip already
    in every `previews/` folder on this machine is named by the old one and none of them will be
    hit again. They are not deleted — a stale entry is inert, which is the whole point of naming
    rather than comparing — so the cost is one render per Shot a Director next looks at (~116 ms
    each here) and the disk the old clips go on occupying until the folder is emptied. That is
    the fix for the Scanlines clips above, not a side effect of it: they are exactly the clips
    that must not be served again.

    Two states that compose to one chain are therefore one name, which is the rule this always
    stated, applied where it is actually decidable: a card disabled, a card left at a value that
    composes nothing, and `1` against `1.0` all reach one clip because they all reach one filter
    string. The two groups are hashed as a pair rather than run together, because `scale` sits
    between them (`EffectStages`) and a stage that moved from one group to the other would be a
    different picture.

    **What it composes with, and why those are the right arguments.** The geometry is the
    preview's own, which is already two of the eight inputs. `clip_offset` is zero and the span
    is `window_duration`: a preview is always a whole Shot from its own first frame, never one
    half of a resolved overlap, so both are read off inputs this already hashes rather than taken
    on trust from a caller. `reference_width` is the **export's** width and the only argument
    here that is not the preview's own: it is what tells the five pixel-denominated composers
    that this grid is half the one their numbers were written for, and it is not a ninth
    fingerprint input because the chain it produces already is one — two geometries that scale a
    block differently compose different text and therefore name different clips, which is exactly
    what the eighth slot's `geometry` was already saying. A stack that does not compose — an
    unknown effect, a look whose `.cube` has gone — raises `EffectRefusal` here rather than
    returning a name. Its one caller
    composes the same chain from the same arguments a few lines earlier and has already refused
    in the catalogue's own words, so that path is unreachable from the route; a direct caller
    gets that sentence rather than a name for a picture that cannot be made.

    **The boundary: a look's file *content* is not in here.** The chain carries the `.cube`'s
    path, so switching looks moves the name and rewriting one in place does not — that clip stays
    cached and stale until the folder is emptied. Closing it means reading and hashing every
    referenced look on every request, cache hit included, and bypassing the `discovered_looks`
    hold that exists so a picker does not re-read 44 MB: measured 2026-08-26 at 0.56 ms for a
    33-cube and 4.24 ms for a 64-cube, against a ~116 ms render. It is left open deliberately,
    and named here so the next reader does not have to measure it to find out.
    """
    chain = build_effect_stages(
        stack,
        width=width,
        height=height,
        luts=luts,
        clip_offset=0.0,
        shot_seconds=window_duration,
        reference_width=reference_width,
    )
    fields = (
        _canonical(take),
        f"{_number(window_start)}+{_number(window_duration)}",
        _number(offset),
        _canonical([list(chain.geometry), list(chain.treatment)]),
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
