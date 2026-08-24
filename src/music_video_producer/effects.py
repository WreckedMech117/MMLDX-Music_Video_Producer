"""The one place a derived artefact is tied to the thing it was derived from.

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
"""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = [
    "FINGERPRINT_CHUNK_BYTES",
    "fingerprint_size",
    "song_fingerprint",
    "song_fingerprints_match",
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
