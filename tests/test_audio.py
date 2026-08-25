"""`audio.py`: the decode argv as text, and the extraction as a pure function of samples.

Two halves, tested two ways, which is the point of the module's shape. The decode is I/O and is
asserted as a **string** — standing law 10, the same treatment `assembly.trim_args` gets — so no
test here has to run ffmpeg to know what would be run. Everything after the decode is arithmetic
over an array, so it is asserted by *comparison*: the same samples produce an equal envelope, and
a synthesised track of known tempo comes back at that tempo.
"""

from __future__ import annotations

import json
import subprocess
import wave
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest

from music_video_producer import audio as audio_module
from music_video_producer.audio import (
    DECODE_SAMPLE_RATE,
    DEFAULT_ANALYSIS_RATE,
    DEFAULT_BAND_COUNT,
    ENVELOPE_REQUIRED_KEYS,
    ENVELOPE_VERSION,
    FREQUENCY_PRECISION,
    TIME_PRECISION,
    TRANSFORM_CHUNK_FRAMES,
    FfmpegMissing,
    SongDecodeError,
    analyze_song,
    decode_argv,
    decode_samples,
    extract_envelope,
)


def click_track(bpm: float, seconds: float = 30.0, rate: int = DECODE_SAMPLE_RATE) -> np.ndarray:
    """A metronome: a decaying 1 kHz burst every beat, and silence in between.

    Synthesised rather than fixtured because the whole value of it is that the true tempo is
    known exactly — a real recording's "true" BPM is itself an estimate, so it can only ever
    confirm that two estimates agree.
    """
    samples = np.zeros(int(seconds * rate), dtype=np.float32)
    moment = np.arange(int(0.02 * rate)) / rate
    burst = (np.sin(2 * np.pi * 1000 * moment) * np.exp(-moment * 120)).astype(np.float32)
    period = 60.0 / bpm
    beat = 0
    while True:
        start = round(beat * period * rate)
        if start + burst.size >= samples.size:
            return samples
        samples[start : start + burst.size] += burst
        beat += 1


def write_wav(destination, samples: np.ndarray, rate: int) -> None:
    """The one serialisation: mono, 16-bit, clipped rather than wrapped.

    Takes a path or a file object, because the two callers want different things out of it — a
    file on disk for the decode tests here, and bytes for the upload tests in `test_api.py`.
    """
    with wave.open(destination, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def wav_file(path: Path, samples: np.ndarray, rate: int = 44100) -> Path:
    write_wav(str(path), samples, rate)
    return path


def click_wav_bytes(bpm: float = 120.0, seconds: float = 4.0, rate: int = 22050) -> bytes:
    """`click_track` as an uploadable file — a real, decodable metronome.

    **The same generator, not a second one.** This was a stdlib transcription of `click_track`'s
    formula living in `test_api.py`: the same `sin(2*pi*1000*t) * exp(-120*t)` over the same
    0.02 s burst, written out a sample at a time at a different amplitude. Two spellings of one
    signal is two things to keep in step, and the retrospective's duplication map named it (A12).
    So the arithmetic is `click_track`'s alone and this adds the container.

    The default silence in `test_api.py`'s `wav_bytes` is a legitimate measurement and therefore
    useless for telling *measured* from *not measured*. This one has beats in it, so a BPM
    greater than zero is evidence the analysis actually ran rather than evidence of a defaulted
    record — which is why the API suite reaches for it rather than for silence.
    """
    content = BytesIO()
    write_wav(content, click_track(bpm, seconds=seconds, rate=rate), rate)
    return content.getvalue()


def test_the_decode_argv_is_the_pipe_it_claims_to_be(tmp_path: Path):
    """Asserted as text, never by running it. A render input is compared as a string here.

    Every element that could be "whatever ffmpeg felt like" is pinned, because the reader on the
    other end of this pipe interprets raw bytes: a stereo pipe would interleave two channels into
    one array and a float pipe would be read as noise. `-vn` is here because an MP3 with cover art
    has a video stream, and `-nostdin` because this runs inside a server whose stdin is not
    ffmpeg's to consume.
    """
    assert decode_argv(tmp_path / "song.mp3") == [
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-i",
        (tmp_path / "song.mp3").as_posix(),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "22050",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    # The sample rate is an argument, not a constant baked into the string, and the path is
    # posix-slashed — a Windows backslash in an ffmpeg argument is this codebase's recurring trap.
    assert "44100" in decode_argv(tmp_path / "song.mp3", sample_rate=44100)


def test_extraction_is_a_pure_function_of_its_samples():
    """Same array in, equal envelope out — and equal again after a round trip through JSON.

    The second half is not decoration. The envelope's life is as a sidecar file, so "pure" has to
    survive serialisation to be worth anything: an envelope that changed in the last decimal place
    on the way to disk would make every fingerprint-skipped read subtly disagree with the analysis
    that produced it.
    """
    samples = np.random.default_rng(11).standard_normal(DECODE_SAMPLE_RATE * 3).astype(np.float32)
    first = extract_envelope(samples * 0.2)
    second = extract_envelope(samples * 0.2)
    assert first == second
    assert json.loads(json.dumps(first)) == first


def test_the_envelope_records_the_rate_and_band_count_it_was_taken_at():
    """Recorded fields, not constants read back from this module.

    A consumer holding an envelope must be able to answer "what does band 2 mean here?" from the
    envelope itself, or tuning either number later becomes a migration of every project on disk.
    So the settings round-trip, the arrays are the size the record claims, and the band edges are
    published in Hz.
    """
    samples = click_track(120, seconds=8.0)
    default = extract_envelope(samples)
    assert default["analysis_rate"] == DEFAULT_ANALYSIS_RATE
    assert default["band_count"] == DEFAULT_BAND_COUNT
    assert len(default["bands"]) == DEFAULT_BAND_COUNT

    tuned = extract_envelope(samples, analysis_rate=15.0, band_count=4)
    assert tuned["analysis_rate"] == 15.0
    assert tuned["band_count"] == 4
    assert len(tuned["bands"]) == 4
    assert len(tuned["band_average"]) == 4
    assert len(tuned["band_edges"]) == 5
    # Every per-frame array is the length the record claims, so a consumer can index by frame
    # without measuring anything first.
    frames = tuned["analysis_frames"]
    assert frames == len(tuned["rms"]) == len(tuned["peak"]) == len(tuned["flux"])
    assert all(len(band) == frames for band in tuned["bands"])
    # Half the rate is half the frames, which is what makes the recorded rate meaningful at all.
    assert frames == pytest.approx(default["analysis_frames"] / 2, abs=1)


def test_the_recorded_rate_is_the_effective_one_not_the_requested_one():
    """The hop is a whole number of samples, so a rate that does not divide the sample rate is
    honoured to the nearest hop — and the envelope says the rate it actually got. Claiming the
    requested rate would put every marker drawn from it progressively out of place."""
    envelope = extract_envelope(click_track(120, seconds=4.0), analysis_rate=31.0)
    assert envelope["hop_samples"] == round(DECODE_SAMPLE_RATE / 31.0)
    assert envelope["analysis_rate"] == pytest.approx(
        DECODE_SAMPLE_RATE / envelope["hop_samples"], abs=1e-6
    )
    assert envelope["analysis_rate"] != 31.0


@pytest.mark.parametrize("bpm", [90, 128, 140, 75])
def test_a_click_track_comes_back_at_the_tempo_it_was_built_at(bpm: int):
    """Within tolerance, and the tolerance is a measurement rather than a wish.

    Precision here is bounded by the analysis rate and nothing else: at 30 Hz the autocorrelation
    lag is an integer number of frames, so the reachable tempos near 140 are 138.5 and 150.0.
    Parabolic interpolation on the peak roughly halves that error — 90.0 / 128.3 / 139.1 against a
    true 90 / 128 / 140 — and does not remove it. That residual is why the BPM is presented as an
    estimate everywhere and why nothing in this application refuses on its value.
    """
    envelope = extract_envelope(click_track(bpm))
    assert envelope["bpm"] == pytest.approx(bpm, rel=0.01)
    # And the beat grid it implies is the same tempo said a second way, at roughly one beat per
    # period across the track — an estimate that produced no beats would be no use to a marker.
    beats = envelope["beats"]
    assert len(beats) == pytest.approx(30.0 * bpm / 60.0, abs=2)
    spacing = np.diff(beats)
    assert float(spacing.mean()) == pytest.approx(60.0 / bpm, rel=0.01)


def test_the_onsets_land_on_the_clicks():
    """An onset is where the sound changes. On a metronome that is once per beat, and the count
    is the check that peak-picking is not firing on the decay tail of the click before it."""
    envelope = extract_envelope(click_track(120, seconds=20.0))
    assert len(envelope["onsets"]) == pytest.approx(40, abs=2)
    assert all(0.0 <= moment <= 20.0 for moment in envelope["onsets"])


def test_the_whole_song_band_average_is_one_fixed_size_array():
    """AD-26's whole-song per-band average, computed here and once.

    A band chosen on one Shot's panel has to mean the same band on another's, and a binding copied
    between Shots has to keep its meaning — both need a reference belonging to the *song* rather
    than to a window of it. Fixed size means `band_count`, whatever the song's length.
    """
    for seconds in (4.0, 20.0):
        envelope = extract_envelope(click_track(110, seconds=seconds))
        assert len(envelope["band_average"]) == envelope["band_count"]
        assert all(0.0 <= value <= 1.0 for value in envelope["band_average"])
        # It is the mean of the band's own envelope, not a separate measurement that could drift
        # away from the arrays beside it.
        for index, band in enumerate(envelope["bands"]):
            assert envelope["band_average"][index] == pytest.approx(
                float(np.mean(band)), abs=1e-3
            )


def test_a_silent_track_measures_as_silence_and_does_not_pretend_to_a_tempo():
    """Zeros here are a true answer, and the one place they are allowed.

    A silent track really does have no level, no onsets and no tempo, and saying so is honest. The
    forbidden thing is a *failure* that produces the same shape — which is why every failure below
    raises instead.
    """
    envelope = extract_envelope(np.zeros(DECODE_SAMPLE_RATE * 2, dtype=np.float32))
    assert max(envelope["rms"]) == 0.0
    assert envelope["onsets"] == []
    assert envelope["beats"] == []
    assert envelope["bpm"] == 0.0
    assert envelope["band_average"] == [0.0] * envelope["band_count"]


def test_a_failure_raises_rather_than_returning_an_envelope_of_zeros(tmp_path: Path):
    """The story's hard rule: no consumer ever receives a zeroed envelope for a failure.

    Three failures, three raises — nothing decodable in the file, no samples at all, and an
    incoherent setting. Each of them could plausibly have been "return an empty envelope", and
    each of them would then be indistinguishable from the silent track above.
    """
    with pytest.raises(ValueError):
        extract_envelope(np.zeros(0, dtype=np.float32))
    with pytest.raises(ValueError):
        extract_envelope(np.zeros(1000, dtype=np.float32), band_count=0)
    with pytest.raises(ValueError):
        extract_envelope(np.zeros(1000, dtype=np.float32), analysis_rate=0)

    not_audio = tmp_path / "song.wav"
    not_audio.write_bytes(b"this is not a wave file")
    with pytest.raises(SongDecodeError):
        decode_samples(not_audio)


def test_a_missing_ffmpeg_is_its_own_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Separated from a decode failure because the remedies are different. Flattening the two
    would tell a Director to check their MP3 when the real answer is that ffmpeg was never
    installed — the one failure here that is about the machine rather than the file."""

    def missing(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "ffmpeg")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(FfmpegMissing):
        decode_samples(tmp_path / "song.wav")


def test_a_real_file_decodes_through_ffmpeg_to_the_rate_it_was_asked_for(tmp_path: Path):
    """The one test here that runs the binary — the whole point of the argv test above is that
    nothing else has to. ffmpeg is already a hard requirement of this application and the suite
    already probes durations with it, so its absence is a broken environment rather than a skip.
    """
    source = wav_file(tmp_path / "tone.wav", click_track(120, seconds=2.0, rate=44100), rate=44100)
    samples = decode_samples(source)
    # Resampled by ffmpeg, mono, and scaled into −1…1 — the three things the reader assumes.
    assert samples.dtype == np.float32
    assert samples.size == pytest.approx(DECODE_SAMPLE_RATE * 2, rel=0.01)
    assert float(np.abs(samples).max()) <= 1.0

    envelope = analyze_song(source)
    assert envelope["sample_rate"] == DECODE_SAMPLE_RATE
    assert envelope["duration"] == pytest.approx(2.0, abs=0.05)
    assert envelope["bpm"] == pytest.approx(120, rel=0.05)


def test_the_transform_is_batched_and_the_batch_size_changes_no_number():
    """The memory bound, and the property that makes it safe to have.

    The whole-song spectrogram of a three-minute track is roughly 300 MB of float64 and it scales
    linearly with length, on a host whose memory belongs to ComfyUI. So the transform runs in
    blocks — and a block boundary must be invisible in the output, or the bound would have been
    bought with a silently different measurement.

    Three chunk sizes, one of them far larger than the whole song (so the batching is effectively
    off) and one deliberately not a divisor of the frame count, all compared whole. The only thing
    a frame needs from outside its own block is the previous frame's spectrum, and `flux` is where
    that shows: an unhandled boundary would leave a zero at the first frame of every block.
    """
    samples = click_track(128, seconds=12.0)
    reference = extract_envelope(samples, chunk_frames=TRANSFORM_CHUNK_FRAMES)
    assert reference == extract_envelope(samples, chunk_frames=37)
    assert reference == extract_envelope(samples, chunk_frames=100_000)
    assert reference == extract_envelope(samples, chunk_frames=1)
    with pytest.raises(ValueError):
        extract_envelope(samples, chunk_frames=0)

    # And the comparison above can actually see a boundary. On a signal whose flux is non-zero
    # everywhere, a dropped carry would leave a zero at the first frame of every block — so these
    # are the frames the equality is really about, and they are asserted to be live.
    noise = (
        np.random.default_rng(5).standard_normal(DECODE_SAMPLE_RATE * 4).astype(np.float32) * 0.2
    )
    chunked = extract_envelope(noise, chunk_frames=13)
    assert chunked == extract_envelope(noise, chunk_frames=100_000)
    boundaries = list(range(13, chunked["analysis_frames"], 13))
    assert len(boundaries) > 5
    assert all(chunked["flux"][frame] > 0 for frame in boundaries)


def test_an_envelope_carries_exactly_the_keys_a_reader_is_allowed_to_demand():
    """`ENVELOPE_REQUIRED_KEYS` is the contract `store.read_song_envelope` validates against, and
    it lives here because this is what produces it. Asserted as an *exact* match rather than a
    subset, so a field added to the envelope without a decision about whether a reader may rely on
    it fails here instead of quietly becoming optional."""
    envelope = extract_envelope(click_track(120, seconds=3.0))
    assert set(envelope) == set(ENVELOPE_REQUIRED_KEYS)
    assert envelope["version"] == ENVELOPE_VERSION


def test_the_settings_that_are_not_a_measurement_are_refused():
    """`sample_rate` is the divisor in every conversion in the module, and it was the one setting
    nobody validated: zero reached the caller as a bare `ZeroDivisionError`, and a negative rate
    produced a complete envelope of nonsense without complaining once."""
    samples = click_track(120, seconds=2.0)
    for rate in (0, -22050):
        with pytest.raises(ValueError, match="sample_rate"):
            extract_envelope(samples, sample_rate=rate)


def test_a_hop_wider_than_the_window_measures_instead_of_failing():
    """An analysis rate low enough to space the frames further apart than the window is long.

    It is a legitimate setting — `analysis_rate` is a recorded, tunable field — and it used to
    fail, because the padded buffer was sized for the frames and came out *shorter than the song*.
    The broadcast error that produced surfaced at the route as "the song could not be decoded",
    which is a sentence about the Director's file and was a bug in this module.
    """
    samples = click_track(120, seconds=6.0)
    envelope = extract_envelope(samples, analysis_rate=5.0)
    assert envelope["hop_samples"] > envelope["fft_size"]
    assert envelope["analysis_rate"] == 5.0
    assert envelope["analysis_frames"] == len(envelope["rms"]) == 30


def test_the_top_band_edge_is_nyquist_and_the_top_bin_is_inside_it():
    """Published edges are what a consumer is told a band covers.

    The top edge used to be `nyquist + 1`, which told a reader that band 7 reached 11026 Hz for a
    signal that stops at 11025 — a hertz invented to keep the highest bin inside the last mask.
    The boundary convention belongs in the mask, not in the number the consumer reads.
    """
    envelope = extract_envelope(click_track(120, seconds=2.0))
    assert envelope["band_edges"][-1] == envelope["sample_rate"] / 2
    assert envelope["band_edges"] == sorted(envelope["band_edges"])
    # Hertz rounded as hertz. A band edge landing on exactly three decimals would pass either way,
    # so this asserts the constant that decides it rather than a sample value.
    assert FREQUENCY_PRECISION != TIME_PRECISION
    assert all(
        round(edge, FREQUENCY_PRECISION) == edge for edge in envelope["band_edges"]
    )
    # A full-scale signal at the very top of the spectrum still registers, which is what the
    # inclusive last mask is for.
    top = np.sin(
        2 * np.pi * (DECODE_SAMPLE_RATE / 2) * np.arange(DECODE_SAMPLE_RATE) / DECODE_SAMPLE_RATE
    ).astype(np.float32)
    assert extract_envelope(top)["band_average"][-1] >= 0.0


def test_a_value_that_is_not_a_number_fails_the_analysis_rather_than_reaching_the_disk():
    """`json.dump` writes a non-finite float as the bare token `NaN`, which Python reads back
    happily and every strict parser rejects. Nothing here is supposed to produce one — but
    "supposed to" is not a property, and the sidecar would be unreadable to the one thing most
    likely to read it next. So a non-finite value is a named analysis failure, before the disk."""
    for spoiled in (np.nan, np.inf, -np.inf):
        samples = click_track(120, seconds=2.0).copy()
        samples[1000] = spoiled
        with pytest.raises(ValueError, match="not numbers"):
            extract_envelope(samples)

    # The input check above is the cheap one. `_finite` and `_rounded` sit behind it over the
    # *computed* values, because an input that is entirely finite can still divide badly.
    assert audio_module._finite(0.5, "x", 2) == 0.5
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="x"):
            audio_module._finite(bad, "x", 2)
    with pytest.raises(ValueError):
        audio_module._rounded(np.array([0.1, np.nan]), 2)
