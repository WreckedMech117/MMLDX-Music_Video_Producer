"""Whisper over the master song: the one place this application hears the track.

The measurement that motivated it (2026-08-19): five shots were marked `singing` over
windows the track leaves instrumental, and H3 — told to sing over a voiceless reference —
invented its own words and lipsynced to the invention. Word timestamps answer both of the
Director's questions at once: where the voice is at all (`Song.vocal_spans`) and where
each `[Tag]` block of the sheet lands (`timeline.align_lyric_blocks`).

Deliberately CPU: the GPU belongs to renders, and a one-time three-to-four-minute
transcription beside a ninety-minute batch is the right trade. `medium` rather than
`small` because `small` with VAD missed the first forty seconds of sung vocals outright
on this project's own track — measured, not assumed. The import is lazy so the
application starts (and every test runs) without the dependency's native libraries
loaded; only the route that transcribes pays for them.
"""

from __future__ import annotations

from pathlib import Path

WHISPER_MODEL = "medium"


def transcribe_song_words(path: Path) -> list[tuple[str, float, float]]:
    """Every word Whisper hears in the file, as (text, start, end) seconds.

    `condition_on_previous_text=False` and no VAD filter, both measured on this project's
    own master (2026-08-19): VAD swallowed the verse under heavy instrumentation, and
    conditioning let one mistranscribed refrain rewrite the next.
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        str(path),
        word_timestamps=True,
        vad_filter=False,
        language="en",
        condition_on_previous_text=False,
    )
    words: list[tuple[str, float, float]] = []
    for segment in segments:
        for word in segment.words or []:
            words.append((word.word.strip(), round(word.start, 2), round(word.end, 2)))
    return words


def merge_vocal_spans(
    words: list[tuple[str, float, float]], *, gap: float = 0.75
) -> list[tuple[float, float]]:
    """Word times merged into voice spans: consecutive words closer than ``gap`` join.

    0.75 s is the merge the singing-flag evidence was measured with; a bigger gap turns
    breaths between lines into separate spans and a smaller one glues verses across rests.
    """
    spans: list[list[float]] = []
    for _text, start, end in sorted(words, key=lambda item: item[1]):
        if spans and start - spans[-1][1] <= gap:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    return [(start, end) for start, end in spans]
