"""Measure the master song once: levels, per-band energy, onsets, beats, a tempo estimate.

Until this module existed the application knew a song's *duration* and, after a Whisper pass,
where the words fell. It had never measured the audio itself, so every later act that wants to
move with the music — a beat marker, a beat-snapped cut, a parameter driven by the bass — had
nothing to resolve against. This is that measurement, and the shape of it is deliberate:

**One decode, then pure arithmetic.** `decode_samples` is the only I/O in the file. Everything
after it is a function of the sample array and the four recorded settings, which is what makes
`extract_envelope` assertable by comparing two results rather than by mocking anything. The
decode argv lives in its own builder for the same reason `assembly.trim_args` does — a render
input is compared as text, never exercised by running it (standing law 10).

**ffmpeg, not a decoder dependency.** The binary is already a hard requirement of this
application; adding a Python audio decoder to read a file ffmpeg reads anyway would be a second
codec surface to keep in step with the first. The pipe is `s16le`, mono, at
`DECODE_SAMPLE_RATE` — resampling in ffmpeg rather than here because ffmpeg's resampler is
better than anything worth writing in this file.

**The rate and the band count are recorded, never read back from code.** They are written onto
every envelope, and every consumer must read them from the envelope it is holding. That is what
makes tuning them a re-analysis rather than a migration, and it is why this module exports no
"the analysis rate" constant that a consumer could import instead. `DEFAULT_ANALYSIS_RATE` and
`DEFAULT_BAND_COUNT` are the *defaults a new analysis is taken at*, and nothing else.

**A failure is never an envelope of zeros.** Every failure path raises. A silent track really
does measure as zeros and that is a true answer; a missing binary, an undecodable file or an
empty decode are not answers at all, and returning a plausible-looking envelope for them would
put a lie in the sidecar that no later reader could detect.

Nothing here imports `app`, `batch` or `assembly`, and nothing here touches a manifest.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "DECODE_SAMPLE_RATE",
    "DECODE_TIMEOUT_SECONDS",
    "DEFAULT_ANALYSIS_RATE",
    "DEFAULT_BAND_COUNT",
    "ENVELOPE_REQUIRED_KEYS",
    "ENVELOPE_VERSION",
    "FFT_SIZE",
    "FfmpegMissing",
    "SongAnalysisError",
    "SongDecodeError",
    "analyze_song",
    "decode_argv",
    "decode_samples",
    "extract_envelope",
]

#: Envelopes are analysed at this many frames per second unless a caller asks otherwise. 30 Hz
#: is the spine's working assumption: fine enough that a beat lands within ~17 ms of true, and
#: coarse enough that a three-minute song is a few thousand frames rather than a few hundred
#: thousand. It bounds the tempo estimate's precision — see `_estimate_tempo` — and raising it
#: is an "Ask First" change, not a tuning knob.
DEFAULT_ANALYSIS_RATE = 30.0

#: How many frequency bands a per-band envelope is split into. Eight, log-spaced, so "band 2"
#: means low-mid to a human rather than an arbitrary slice of a linear spectrum.
DEFAULT_BAND_COUNT = 8

#: What the decode resamples to. 22.05 kHz keeps everything up to ~11 kHz, which is well past
#: anything an energy envelope or an onset detector cares about, and halves the decode and the
#: transform against 44.1 kHz for no measurable difference in the result.
DECODE_SAMPLE_RATE = 22050

#: Transform size. 2048 samples at 22.05 kHz is ~93 ms — the usual trade, and long enough that
#: the lowest band has bins in it at all.
FFT_SIZE = 2048

#: The whole decode, bounded. A three-minute master decodes in well under a second; a bound
#: this loose only ever fires on something that is not going to finish.
DECODE_TIMEOUT_SECONDS = 180

#: Bumped whenever the *meaning* of a field here changes, so a sidecar written by an older
#: build is recognisable as such rather than being silently misread. It is not a schema version
#: to branch on: this application has exactly one reader, and the honest response to an
#: unfamiliar version is to report the analysis absent and re-take it.
ENVELOPE_VERSION = 1

#: Every key `extract_envelope` puts in an envelope, and therefore the shape a reader may demand
#: before believing a file is one. It lives here rather than in `store.py` because this module is
#: what produces it: a contract kept next to its consumer drifts the first time the producer gains
#: a field, and drifts silently. `store.ProjectStore.read_song_envelope` imports it, which is why
#: this module must stay a leaf — it imports nothing of this application's, and nothing about that
#: may change.
#:
#: Pinned by a test asserting this is *exactly* the key set a real envelope has, so a key added
#: below without a decision about it fails rather than quietly becoming optional.
ENVELOPE_REQUIRED_KEYS = frozenset(
    {
        "version",
        "analysis_rate",
        "band_count",
        "sample_rate",
        "fft_size",
        "hop_samples",
        "analysis_frames",
        "duration",
        "band_edges",
        "band_scale",
        "rms",
        "peak",
        "flux",
        "bands",
        "band_average",
        "onsets",
        "beats",
        "bpm",
    }
)

#: The band split's floor and the tempo search's bounds. The floor is below the lowest note on a
#: five-string bass; the tempo range covers everything from a slow ballad to drum and bass, and
#: exists so autocorrelation cannot answer "0.4 BPM" from a slow swell.
BAND_FLOOR_HZ = 20.0
MIN_BPM = 50.0
MAX_BPM = 200.0

#: Where the tempo prior sits and how wide it is, in octaves. Autocorrelation cannot tell a
#: tempo from half of it — a 128 BPM track has a real peak at 64 — so something has to break the
#: tie, and a log-normal prior around a typical tempo is the standard answer. Wide enough that
#: it never overrules a clear peak, narrow enough to settle a genuine ambiguity.
TEMPO_PRIOR_CENTRE_BPM = 120.0
TEMPO_PRIOR_WIDTH_OCTAVES = 0.9

#: Peak-picking on the flux. The threshold is a local mean plus a fixed margin (flux is
#: normalised to a 0–1 peak, so the margin is meaningful in absolute terms), and two onsets
#: closer than the gap are one event — a snare and its own ring are not two hits.
ONSET_WINDOW_FRAMES = 15
ONSET_MARGIN = 0.1
ONSET_MIN_GAP_SECONDS = 0.07

#: Decimal places the envelope's arrays are rounded to before they are written. Levels are in
#: 0–1, so four places is finer than the ear and finer than the float32 the transform produced;
#: times are in seconds, so three places is a millisecond. Rounding is not a size strategy — the
#: measurement is that trimming precision saves about 1% — it is what makes an envelope that
#: went through JSON compare equal to the one that came out of this module.
LEVEL_PRECISION = 4
TIME_PRECISION = 3
#: Hertz, where a hundredth is far finer than any band edge needs. A third constant rather
#: than reusing `TIME_PRECISION`, because that one is documented as a millisecond and a
#: constant named for one unit applied to another's values is how a band edge silently
#: acquires millisecond precision.
FREQUENCY_PRECISION = 2

#: How many analysis frames are transformed at a time. The transform is the only part of this
#: module whose working set grows with the length of the song, and left unbounded it is the
#: one thing here that could hurt a machine which is also running ComfyUI: the whole-song
#: spectrogram of a three-minute track is roughly 300 MB of float64 and it scales linearly, so
#: a long set would ask for gigabytes on a host whose memory belongs to the renderer. Batching
#: bounds it to this many frames whatever the length (about 6 MB at the default settings) and
#: changes no number in the output: the only thing a frame needs from outside its own block is
#: the previous frame's spectrum, and that is carried across the boundary explicitly. Pinned
#: by a test that measures the same signal at two different chunk sizes and compares.
TRANSFORM_CHUNK_FRAMES = 256


class SongAnalysisError(RuntimeError):
    """Analysis could not be taken. Every subclass names why in its message.

    Deliberately not an `HTTPException` and deliberately not swallowed here: this module has no
    opinion about what a route should do with a song it could not measure, and the two callers
    that exist want different things (an import carries on, a re-analysis reports).
    """


class FfmpegMissing(SongAnalysisError):
    """The ffmpeg binary is not on PATH. Distinct because the remedy is: install ffmpeg."""


class SongDecodeError(SongAnalysisError):
    """ffmpeg ran and produced no usable audio — a corrupt file, a video with no audio track."""


def decode_argv(source: Path, *, sample_rate: int = DECODE_SAMPLE_RATE) -> list[str]:
    """The ffmpeg command that pipes `source` out as mono signed 16-bit PCM on stdout.

    A builder rather than an inline list, so it is asserted by string comparison exactly as
    `assembly.trim_args` and `probe_take_args` are. Nothing in the pipeline is allowed to be
    "whatever ffmpeg felt like": the sample rate, the channel count and the sample format are
    all stated, because a stereo or float pipe would be read as noise by the reader below.

    `-nostdin` because this runs inside a server process whose stdin is not ffmpeg's to consume,
    and `-vn` because an MP3 with embedded cover art has a video stream that is a picture.
    """
    return [
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-i",
        source.as_posix(),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]


def decode_samples(
    source: Path,
    *,
    sample_rate: int = DECODE_SAMPLE_RATE,
    timeout: float = DECODE_TIMEOUT_SECONDS,
) -> np.ndarray:
    """The whole track as mono float32 in −1…1. The only I/O in this module.

    Raises rather than returning an empty array, and the two failures are separate classes
    because they are separate problems: `FfmpegMissing` is about this machine, `SongDecodeError`
    is about this file. A route that flattened them would tell a Director to check their MP3
    when the real answer is that ffmpeg was never installed.
    """
    try:
        result = subprocess.run(
            decode_argv(source, sample_rate=sample_rate),
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise FfmpegMissing("ffmpeg was not found on PATH") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise SongDecodeError(detail[-1] if detail else "ffmpeg could not decode the file") from error
    except subprocess.SubprocessError as error:
        raise SongDecodeError(f"{type(error).__name__} decoding the file") from error
    raw = result.stdout
    # An odd trailing byte is a truncated frame, not a sample; dropping it is the only way to
    # read the rest, and keeping it would shift every subsequent sample by a byte.
    usable = len(raw) - (len(raw) % 2)
    if usable <= 0:
        raise SongDecodeError("the file decoded to no audio at all")
    samples = np.frombuffer(raw[:usable], dtype="<i2").astype(np.float32) / 32768.0
    return samples


def extract_envelope(
    samples: np.ndarray,
    *,
    sample_rate: int = DECODE_SAMPLE_RATE,
    analysis_rate: float = DEFAULT_ANALYSIS_RATE,
    band_count: int = DEFAULT_BAND_COUNT,
    fft_size: int = FFT_SIZE,
    chunk_frames: int = TRANSFORM_CHUNK_FRAMES,
) -> dict[str, Any]:
    """The Song Envelope, as a plain JSON-ready dict. A pure function of `samples`.

    Pure in the sense that matters for testing it: the same array and the same settings produce
    an equal dict, every time, with no clock, no filesystem and no randomness anywhere in the
    path. That is asserted by comparison rather than by inspection, which is the only way to
    pin an output of several thousand floats.

    The envelope records what it was taken at — `analysis_rate`, `band_count`, `sample_rate`,
    `fft_size`, and the band edges in Hz — because a consumer holding an envelope must be able
    to answer "what does band 2 mean here?" without importing this module's defaults. The
    recorded `analysis_rate` is the *effective* one: the hop is an integer number of samples, so
    a requested rate that does not divide the sample rate is honoured to the nearest hop and the
    envelope says so rather than claiming the rate that was asked for.

    `band_average` is AD-26's whole-song per-band average, computed here and once. A band chosen
    on one Shot's panel has to mean the same band on another's, and a binding copied between
    Shots has to keep its meaning; both need a reference that belongs to the song rather than to
    a window of it.
    """
    if sample_rate <= 0:
        # Checked first and checked at all: it is the divisor in every conversion below, so
        # zero reached the caller as a bare ZeroDivisionError and a negative rate produced a
        # whole envelope of nonsense without complaining once.
        raise ValueError("sample_rate must be positive")
    if analysis_rate <= 0:
        raise ValueError("analysis_rate must be positive")
    if band_count <= 0:
        raise ValueError("band_count must be positive")
    if fft_size <= 0 or fft_size % 2:
        raise ValueError("fft_size must be a positive even number")
    if chunk_frames <= 0:
        raise ValueError("chunk_frames must be positive")
    signal = np.asarray(samples, dtype=np.float32).reshape(-1)
    if signal.size == 0:
        raise ValueError("cannot measure an empty song")
    if not np.isfinite(signal).all():
        # Refused here rather than allowed to propagate. A single NaN in the input spreads through
        # the transform into every frame that overlaps it and then into the tempo estimate, and
        # what reaches the disk is an envelope-shaped file full of `NaN` tokens that are not JSON.
        # Two milliseconds on a three-minute track to know the input is numbers.
        raise ValueError("the decoded audio contains values that are not numbers")

    hop = max(1, round(sample_rate / analysis_rate))
    effective_rate = sample_rate / hop
    analysis_frames = max(1, math.ceil(signal.size / hop))
    # Long enough for both things it has to be long enough for, which is why it is a `max` and
    # not the obvious expression. It must hold the signal itself, and it must yield at least
    # `analysis_frames` windows. Those coincide while the hop is narrower than the window and
    # come apart the moment it is not: at an analysis rate of 5 Hz the hop is 4410 samples
    # against a 2048-sample window, the first expression is *shorter* than the signal, and the
    # copy below failed with a broadcast error that reached the route as `could not decode`.
    padded = np.zeros(
        max((analysis_frames - 1) * hop + fft_size, signal.size + fft_size), dtype=np.float32
    )
    padded[: signal.size] = signal
    frames = np.lib.stride_tricks.sliding_window_view(padded, fft_size)[:: hop][:analysis_frames]

    window = np.hanning(fft_size).astype(np.float64)
    edges = _band_edges(band_count, sample_rate)
    masks = _band_masks(edges, sample_rate, fft_size)

    rms = np.zeros(analysis_frames, dtype=np.float64)
    peak = np.zeros(analysis_frames, dtype=np.float64)
    flux = np.zeros(analysis_frames, dtype=np.float64)
    bands = np.zeros((band_count, analysis_frames), dtype=np.float64)
    # The spectrum of the last frame of the previous block, and the whole of what one block needs
    # from another. Carrying it explicitly is what makes the batching invisible in the output:
    # `flux` at a block boundary is the same subtraction it would have been in one long array.
    carried: np.ndarray | None = None
    for start in range(0, analysis_frames, chunk_frames):
        stop = min(start + chunk_frames, analysis_frames)
        block = frames[start:stop]
        rms[start:stop] = np.sqrt(np.mean(np.square(block, dtype=np.float64), axis=1))
        peak[start:stop] = np.max(np.abs(block), axis=1)
        spectrum = np.abs(np.fft.rfft(block * window, axis=1))
        if carried is not None:
            flux[start] = float(np.sum(np.maximum(spectrum[0] - carried, 0.0)))
        if stop - start > 1:
            flux[start + 1 : stop] = np.sum(
                np.maximum(np.diff(spectrum, axis=0), 0.0), axis=1
            )
        for index, selected in enumerate(masks):
            bands[index, start:stop] = np.sqrt(
                np.mean(np.square(spectrum[:, selected]), axis=1)
            )
        carried = spectrum[-1]
    # A spectral-flux *proxy*, named that way in the spine and meant literally: the summed
    # positive change in magnitude between consecutive frames, normalised to its own peak. It
    # is not calibrated against anything and no absolute value of it means anything; what it is
    # for is finding where the sound changes, which is what the onset and tempo passes read.
    # Frame 0 keeps its zero: it has no predecessor to have changed from.
    flux = _normalised(flux)

    # One divisor for every band rather than one per band: the bands have to stay comparable
    # with each other, or "the bass is loud" becomes a statement about normalisation. The
    # divisor is recorded so the raw magnitudes are recoverable.
    band_scale = float(bands.max()) if bands.size and bands.max() > 0 else 1.0
    bands = bands / band_scale

    onsets = _pick_onsets(flux, effective_rate)
    bpm, beats = _estimate_tempo(flux, effective_rate)

    return {
        "version": ENVELOPE_VERSION,
        "analysis_rate": round(float(effective_rate), 6),
        "band_count": int(band_count),
        "sample_rate": int(sample_rate),
        "fft_size": int(fft_size),
        "hop_samples": int(hop),
        # `analysis_frames`, never `frame_count`. In this codebase a *frame* is a video frame —
        # the H3 grid, `ASSEMBLY_FPS`, the assembled cut that must match the song within one of
        # them — and an analysis frame at 30 Hz is a different unit entirely. Naming this
        # `frame_count` would put the two in one word, which is the confusion the frame-grid law
        # is most expensive to lose.
        "analysis_frames": int(analysis_frames),
        "duration": _finite(signal.size / sample_rate, "duration", TIME_PRECISION),
        "band_edges": [_finite(edge, "band_edges", FREQUENCY_PRECISION) for edge in edges],
        "band_scale": _finite(band_scale, "band_scale", 9),
        "rms": _rounded(rms, LEVEL_PRECISION),
        "peak": _rounded(peak, LEVEL_PRECISION),
        "flux": _rounded(flux, LEVEL_PRECISION),
        "bands": [_rounded(row, LEVEL_PRECISION) for row in bands],
        "band_average": [
            _finite(row.mean(), "band_average", LEVEL_PRECISION) for row in bands
        ],
        "onsets": _rounded(onsets, TIME_PRECISION),
        "beats": _rounded(beats, TIME_PRECISION),
        "bpm": bpm,
    }


def analyze_song(
    source: Path,
    *,
    sample_rate: int = DECODE_SAMPLE_RATE,
    analysis_rate: float = DEFAULT_ANALYSIS_RATE,
    band_count: int = DEFAULT_BAND_COUNT,
    fft_size: int = FFT_SIZE,
) -> dict[str, Any]:
    """Decode one audio file and measure it. Raises `SongAnalysisError` if it cannot.

    The entry point, and deliberately a plain function of a path: Treatment Story 16.2 folds
    this and the lyric-structure pass under one trigger without merging them, so this must stay
    callable from somewhere that is not the import route.
    """
    return extract_envelope(
        decode_samples(source, sample_rate=sample_rate),
        sample_rate=sample_rate,
        analysis_rate=analysis_rate,
        band_count=band_count,
        fft_size=fft_size,
    )


def _band_masks(edges: list[float], sample_rate: int, fft_size: int) -> list[np.ndarray]:
    """One boolean bin mask per band, built once and reused by every block of the transform.

    Half-open on every band but the last, which includes Nyquist itself. Without that the topmost
    bin belongs to no band at all, and the highest band under-reports for the sake of a boundary
    convention nobody can see.
    """
    bins = np.fft.rfftfreq(fft_size, 1.0 / sample_rate)
    masks: list[np.ndarray] = []
    for index in range(len(edges) - 1):
        last = index == len(edges) - 2
        selected = (bins >= edges[index]) & (
            bins <= edges[index + 1] if last else bins < edges[index + 1]
        )
        if not selected.any():
            # A band narrower than one bin, which only happens at an absurd `band_count`. Its
            # nearest bin is a better answer than a row of zeros that reads as silence.
            selected = np.zeros_like(bins, dtype=bool)
            selected[min(len(bins) - 1, int(np.searchsorted(bins, edges[index])))] = True
        masks.append(selected)
    return masks


def _band_edges(band_count: int, sample_rate: int) -> list[float]:
    """`band_count + 1` frequencies, log-spaced from `BAND_FLOOR_HZ` to Nyquist.

    Log rather than linear because pitch is: a linear eight-way split of 0–11 kHz puts every
    note a bass or a voice ever plays into band 0, and spends six bands on cymbals.
    """
    nyquist = sample_rate / 2.0
    low = math.log(BAND_FLOOR_HZ)
    high = math.log(max(nyquist, BAND_FLOOR_HZ * 2))
    step = (high - low) / band_count
    edges = [math.exp(low + step * index) for index in range(band_count)]
    # Nyquist itself, and not a hertz above it. The published edges are what a consumer is told
    # band 7 covers, so an edge of `nyquist + 1` claimed the band reached 11026 Hz for a signal
    # that stops at 11025. The topmost bin is kept inside the band by making the last mask
    # inclusive in `_band_masks`, which is where a boundary convention belongs.
    edges.append(nyquist)
    return edges


def _normalised(values: np.ndarray) -> np.ndarray:
    """`values` scaled so its maximum is 1, or left alone when there is nothing to scale.

    An all-zero flux is what a silent track genuinely produces, and it must stay all zero rather
    than becoming a division by zero or an invented 1.
    """
    largest = float(values.max()) if values.size else 0.0
    return values / largest if largest > 0 else values


def _pick_onsets(flux: np.ndarray, analysis_rate: float) -> np.ndarray:
    """Times, in seconds, where the sound changes enough to call it an event.

    A local maximum of the flux that also clears a moving local mean by `ONSET_MARGIN`, with a
    minimum gap enforced afterwards. The moving mean is what makes this work on a track with a
    quiet intro and a loud chorus: a single global threshold either misses every intro hit or
    finds an onset on every chorus frame.
    """
    if flux.size < 3:
        return np.zeros(0, dtype=np.float64)
    window = min(ONSET_WINDOW_FRAMES, flux.size)
    kernel = np.ones(window, dtype=np.float64) / window
    local_mean = np.convolve(flux, kernel, mode="same")
    minimum_gap = max(1, round(ONSET_MIN_GAP_SECONDS * analysis_rate))
    picked: list[int] = []
    for index in range(1, flux.size - 1):
        value = flux[index]
        if value <= flux[index - 1] or value < flux[index + 1]:
            continue
        if value < local_mean[index] + ONSET_MARGIN:
            continue
        if picked and index - picked[-1] < minimum_gap:
            # Keep the louder of the two, so a gap does not systematically prefer whichever
            # event happened to come first.
            if value > flux[picked[-1]]:
                picked[-1] = index
            continue
        picked.append(index)
    return np.asarray(picked, dtype=np.float64) / analysis_rate


def _estimate_tempo(flux: np.ndarray, analysis_rate: float) -> tuple[float, np.ndarray]:
    """One estimated BPM, and the beat grid it implies, in seconds.

    Autocorrelation of the flux over the lags `MIN_BPM`…`MAX_BPM` allow, weighted by a
    log-normal prior around `TEMPO_PRIOR_CENTRE_BPM` to settle the octave, then **parabolic
    interpolation on the winning peak**.

    The interpolation is there because the precision is bounded by the analysis rate and nothing
    else: at 30 Hz the lag is an integer number of frames, so the reachable tempos near 140 BPM
    are 138.5 and 150.0 and there is nothing in between. Measured **here**, against synthesised
    click tracks of known tempo: the integer peak gives 90.0 / 128.6 / 138.5 for a true
    90 / 128 / 140, and the interpolated peak gives 90.0 / 128.3 / 139.1 — about half the
    error, for two subtractions.

    That is the one stated set of figures for this experiment, and `tests/test_audio.py` cites
    it rather than restating its own. Two nearly-equal sets of measurements in two files is how
    a number nobody re-measured survives being quietly wrong; the planning spec's estimate of
    128.4 / 139.0 was close enough to this to hide the difference for exactly that reason.

    It does not remove the error, and that is the point of the AC's wording: the BPM is an
    **estimate** wherever it is shown, and nothing in this application refuses on its value.
    Getting closer would need a higher analysis rate, which is a decision above this module.

    The beat grid's phase is chosen by scoring every whole-frame offset within one period
    against the flux and keeping the best. A phase is not worth interpolating: a beat marker
    drawn a frame early is invisible, where a beat marker drawn on the wrong beat is not.
    """
    empty = np.zeros(0, dtype=np.float64)
    if flux.size < 4 or not np.any(flux):
        return 0.0, empty
    centred = flux - flux.mean()
    correlation = np.correlate(centred, centred, mode="full")[flux.size - 1 :]
    if correlation[0] <= 0:
        return 0.0, empty
    minimum_lag = max(1, math.floor(analysis_rate * 60.0 / MAX_BPM))
    maximum_lag = min(correlation.size - 2, math.ceil(analysis_rate * 60.0 / MIN_BPM))
    if maximum_lag <= minimum_lag:
        return 0.0, empty
    lags = np.arange(minimum_lag, maximum_lag + 1)
    candidate_bpm = 60.0 * analysis_rate / lags
    prior = np.exp(
        -0.5
        * np.square(np.log2(candidate_bpm / TEMPO_PRIOR_CENTRE_BPM) / TEMPO_PRIOR_WIDTH_OCTAVES)
    )
    scored = correlation[minimum_lag : maximum_lag + 1] * prior
    if not np.any(scored > 0):
        return 0.0, empty
    best = int(lags[int(np.argmax(scored))])
    lag = best + _parabolic_offset(
        float(correlation[best - 1]), float(correlation[best]), float(correlation[best + 1])
    )
    if lag <= 0:
        return 0.0, empty
    bpm = round(60.0 * analysis_rate / lag, 1)
    period = analysis_rate * 60.0 / bpm
    beats = _beat_grid(flux, period)
    return bpm, beats / analysis_rate


def _parabolic_offset(before: float, at: float, after: float) -> float:
    """Sub-sample position of a peak sampled at three points, in units of one sample.

    Clamped to ±0.5: a fitted vertex further out than half a sample means the three points are
    not a peak, and following it would move the answer to a lag that was never measured.
    """
    denominator = before - 2.0 * at + after
    if denominator == 0:
        return 0.0
    return max(-0.5, min(0.5, 0.5 * (before - after) / denominator))


def _beat_grid(flux: np.ndarray, period: float) -> np.ndarray:
    """Frame positions of a fixed-period beat grid, phased to wherever the flux agrees most."""
    if period <= 0:
        return np.zeros(0, dtype=np.float64)
    best_offset, best_score = 0.0, -1.0
    for offset in range(max(1, math.ceil(period))):
        positions = np.arange(offset, flux.size, period)
        score = float(flux[np.rint(positions).astype(int).clip(0, flux.size - 1)].sum())
        if score > best_score:
            best_offset, best_score = float(offset), score
    return np.arange(best_offset, flux.size, period)


def _finite(value: float, field: str, places: int) -> float:
    """One rounded float, or a named refusal if it is not a real number.

    Nothing in this module is *supposed* to produce a NaN or an infinity: the divisions are all
    guarded and the input is bounded by the decode. But "supposed to" is not a property, and the
    consequence of being wrong is specific and nasty. `json.dump` writes non-finite values as the
    bare tokens `NaN` and `Infinity`, which are not JSON at all: Python reads them back happily,
    every strict parser rejects the file outright, and the sidecar would be unreadable to the one
    thing most likely to read it next. So a non-finite value fails the analysis, by name, before
    it can reach the disk - and `store.write_song_envelope` passes `allow_nan=False` behind this
    as the second line of the same defence.
    """
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} computed as {number}, which is not a measurement")
    return round(number, places)


def _rounded(values: np.ndarray, places: int) -> list[float]:
    array = np.asarray(values, dtype=np.float64)
    if array.size and not np.isfinite(array).all():
        raise ValueError("an envelope value is not finite, so this is not a measurement")
    return [round(float(value), places) for value in array]
