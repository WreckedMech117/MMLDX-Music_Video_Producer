r"""The reactive drive and the `sendcmd` script it compiles to, asserted as text.

Standing law 10: a generated render input is a pure function of the manifest and is compared as
a string. That is the whole shape of this file, and here it is not a stylistic preference — it is
the only place this feature can be caught being wrong. Measured 2026-08-27 against this project's
ffmpeg 7.0, and reproduced by `test_a_mistargeted_command_is_accepted_and_ignored` below: a
command aimed at a target that does not exist is **silently ignored**, rc 0, no warning even at
`-v warning`, output byte-identical to the same chain undriven. A control wired to an unpinned
compiler would look like it worked, on every screen, for every parameter, forever.

So three things are asserted that nothing else can assert:

* the script's **text**, pinned character for character, including across a Shot that becomes two
  clips — where each clip's script must be right for that clip's own seconds;
* every **target** the compiler emits appears as an `@label` in the chain composed by the *same
  call*, for every drivable parameter in the catalogue;
* the drive table's `option=value` for a driven value appears in the **composed stage text**, so
  the arithmetic a command carries and the arithmetic the chain was built with cannot drift.

Two tests run the real binary, because a pinned string proves the compiler is stable and proves
nothing about whether ffmpeg accepts it.

**The drivable and undrivable tables below are written out by hand.** A test that read them off
the catalogue would pass just as happily for a catalogue that had drifted, and drift here is
invisible: a parameter that silently lost its drive declaration becomes a bind glyph that refuses,
and one that silently gained the wrong filter becomes a binding that does nothing at all.
"""

from __future__ import annotations

import dataclasses
import itertools
import subprocess
from pathlib import Path

import pytest

from music_video_producer.assembly import trim_args
from music_video_producer.effects import (
    BINDING_SPEC_KEYS,
    DRIVE_MODES,
    EFFECT_CATALOGUE,
    ChoiceParameter,
    EffectRefusal,
    LutParameter,
    NumberParameter,
    ParameterBinding,
    StageContext,
    band_series,
    build_effect_stages,
    drive_readout,
    drive_series,
    sendcmd_script,
)

EXPORT_WIDTH = 1056
EXPORT_HEIGHT = 608


# ------------------------------------------------------------------------------------------
# Fixtures written by hand, so nothing here recomputes what it is checking.
# ------------------------------------------------------------------------------------------

#: A two-band envelope at 4 Hz, eight ticks long — two seconds of "song". Small enough that every
#: number in a pinned script can be arrived at with a pencil, and shaped like a real one: band 0
#: is the low band and carries two hits, band 1 is quiet throughout.
#:
#: The rate is 4 and the band count is 2 **on purpose**, because neither is what this application
#: analyses at (30 Hz, 8 bands). `SongAnalysis` records both precisely so an envelope written on
#: another day is read as it was taken, and a compiler that reached for today's constants would
#: pass every test written at today's constants.
ENVELOPE = {
    "version": 1,
    "analysis_rate": 4.0,
    "band_count": 2,
    "bands": [
        [0.10, 0.90, 0.20, 0.10, 0.80, 0.10, 0.30, 0.10],
        [0.00, 0.10, 0.00, 0.00, 0.10, 0.00, 0.00, 0.00],
    ],
}

#: The same shape at the same rate, carrying a **section** rather than hits: band 0 sits at 0.9
#: for four ticks and then drops to nothing for four. `punch` would read one transient at tick 0
#: and nothing else; `sustain` reads a section that arrives, holds, and ends, which is the only
#: shape in which the gate's attack and its release are both visible in one script.
SUSTAIN_ENVELOPE = {
    "version": 1,
    "analysis_rate": 4.0,
    "band_count": 2,
    "bands": [
        [0.90, 0.90, 0.90, 0.90, 0.00, 0.00, 0.00, 0.00],
        [0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00],
    ],
}

#: One second of song at 4 Hz, quiet until its **last** tick, which is a hit.
#:
#: For the clip that outlives its own analysis. An envelope is `ceil`ed to whole analysis frames
#: and a Shot may legitimately end after the last one, so the walk holds the last measured value
#: rather than falling to nothing — and the hit is put on the final tick precisely so that "held"
#: and "fell to nothing" are the two ends of the parameter's range rather than two numbers a
#: reader has to compare digit by digit.
TAIL_ENVELOPE = {
    "version": 1,
    "analysis_rate": 4.0,
    "band_count": 2,
    "bands": [
        [0.10, 0.10, 0.10, 0.90],
        [0.00, 0.00, 0.00, 0.00],
    ],
}

#: Everything the catalogue declares that the music **can** reach, as `(effect, parameter)` ->
#: `(ffmpeg filter, that filter's option names in the order they are written)`.
DRIVABLE: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {
    ("soft_focus", "sigma"): ("gblur", ("sigma",)),
    ("banding_suppression", "threshold"): ("deband", ("1thr", "2thr", "3thr", "4thr")),
    ("bloom", "intensity"): ("blend", ("all_opacity",)),
    ("bloom", "radius"): ("gblur", ("sigma",)),
    ("exposure", "amount"): ("eq", ("brightness",)),
    ("contrast", "amount"): ("eq", ("contrast",)),
    ("saturation", "amount"): ("eq", ("saturation",)),
    ("temperature", "amount"): ("colorbalance", ("rm", "bm")),
    ("tint", "amount"): ("colorbalance", ("gm",)),
    ("lift_gamma_gain", "lift"): ("colorbalance", ("rs", "gs", "bs")),
    ("lift_gamma_gain", "gamma"): ("eq", ("gamma",)),
    ("lift_gamma_gain", "gain"): ("colorbalance", ("rh", "gh", "bh")),
    ("monochrome", "amount"): ("hue", ("s",)),
    ("chroma_split", "shift"): ("chromashift", ("cbh", "crh")),
    ("pixelate", "size"): ("pixelize", ("w", "h")),
    ("edge_treatment", "strength"): ("blend", ("all_opacity",)),
    ("scanlines", "strength"): ("drawgrid", ("c",)),
    ("pixel_shuffle", "amount"): ("blend", ("all_opacity",)),
}

#: Everything it declares that the music **cannot** reach, with the sentence a Director is shown.
#: Written out whole, because a refusal is shown whole and asserted verbatim in this repository.
UNDRIVABLE: dict[tuple[str, str], str] = {
    ("punch_in", "zoom"): (
        "punch_in's zoom cannot be driven by the music: driving it would resize the frame "
        "partway through the clip, which ffmpeg aborts on. Nothing was composed."
    ),
    ("slow_zoom", "zoom"): (
        "slow_zoom's zoom cannot be driven by the music: driving it would resize the frame "
        "partway through the clip, which ffmpeg aborts on. Nothing was composed."
    ),
    ("slow_zoom", "direction"): (
        "slow_zoom's direction cannot be driven by the music: it is not a number. "
        "Nothing was composed."
    ),
    ("handheld_shake", "amplitude"): (
        "handheld_shake's amplitude cannot be driven by the music: driving it would resize the "
        "frame partway through the clip, which ffmpeg aborts on. Nothing was composed."
    ),
    ("handheld_shake", "frequency"): (
        "handheld_shake's frequency cannot be driven by the music: it reaches ffmpeg's crop "
        "filter inside an expression rather than as a value of its own. Nothing was composed."
    ),
    ("dutch_tilt", "angle"): (
        "dutch_tilt's angle cannot be driven by the music: driving it would resize the frame "
        "partway through the clip, which ffmpeg aborts on. Nothing was composed."
    ),
    ("mirror", "axis"): (
        "mirror's axis cannot be driven by the music: it is not a number. Nothing was composed."
    ),
    ("grain", "strength"): (
        "grain's strength cannot be driven by the music: ffmpeg's noise filter takes no runtime "
        "commands. Nothing was composed."
    ),
    ("grain", "seed"): (
        "grain's seed cannot be driven by the music: ffmpeg's noise filter takes no runtime "
        "commands. Nothing was composed."
    ),
    ("vignette", "angle"): (
        "vignette's angle cannot be driven by the music: ffmpeg's vignette filter takes no "
        "runtime commands. Nothing was composed."
    ),
    ("sharpen", "amount"): (
        "sharpen's amount cannot be driven by the music: ffmpeg's unsharp filter takes no "
        "runtime commands. Nothing was composed."
    ),
    ("bloom", "threshold"): (
        "bloom's threshold cannot be driven by the music: it reaches ffmpeg's lutyuv filter "
        "inside an expression rather than as a value of its own. Nothing was composed."
    ),
    ("lut_look", "lut"): (
        "lut_look's lut cannot be driven by the music: it is not a number. Nothing was composed."
    ),
    ("lut_look", "interp"): (
        "lut_look's interp cannot be driven by the music: it is not a number. "
        "Nothing was composed."
    ),
    ("posterize", "levels"): (
        "posterize's levels cannot be driven by the music: it reaches ffmpeg's lutyuv filter "
        "inside an expression rather than as a value of its own. Nothing was composed."
    ),
    ("edge_treatment", "low"): (
        "edge_treatment's low cannot be driven by the music: ffmpeg's edgedetect filter takes "
        "no runtime commands. Nothing was composed."
    ),
    ("edge_treatment", "high"): (
        "edge_treatment's high cannot be driven by the music: ffmpeg's edgedetect filter takes "
        "no runtime commands. Nothing was composed."
    ),
    ("scanlines", "lines"): (
        "scanlines's lines cannot be driven by the music: it reaches ffmpeg's drawgrid filter "
        "inside an expression rather than as a value of its own. Nothing was composed."
    ),
    ("pixel_shuffle", "block"): (
        "pixel_shuffle's block cannot be driven by the music: ffmpeg's shufflepixels filter "
        "takes no runtime commands. Nothing was composed."
    ),
    ("pixel_shuffle", "seed"): (
        "pixel_shuffle's seed cannot be driven by the music: ffmpeg's shufflepixels filter "
        "takes no runtime commands. Nothing was composed."
    ),
}


def bound(
    effect_id: str,
    parameter: str,
    /,
    values: dict[str, object] | None = None,
    enabled: bool = True,
    **binding: object,
) -> dict[str, object]:
    """One stack entry carrying one binding, in the plain shape a manifest will hold.

    The default depth is 0.01 rather than anything larger because a depth is bounded by the span
    of the parameter it drives, and the narrowest span in this catalogue is Banding Suppression's
    0.0499. A helper defaulting to half a unit would refuse on two effects for a reason that has
    nothing to do with what is being tested.
    """
    settings: dict[str, object] = {"parameter": parameter, "drive": "punch", "depth": 0.01}
    settings.update(binding)
    return {
        "effect": effect_id,
        "parameters": dict(values or {}),
        "enabled": enabled,
        "bindings": [settings],
    }


def stages(stack: list[dict[str, object]], **kwargs: object):
    kwargs.setdefault("envelope", ENVELOPE)
    kwargs.setdefault("clip_seconds", 2.0)
    return build_effect_stages(
        stack,
        width=EXPORT_WIDTH,
        height=EXPORT_HEIGHT,
        **kwargs,  # type: ignore[arg-type]
    )


def labels_in(chain: tuple[str, ...]) -> set[str]:
    """Every `class@instance` written into a composed chain, read out of the text.

    Deliberately a scan of the finished strings rather than anything the builder handed back: the
    question this file exists to answer is whether the *text* ffmpeg will read carries the label
    the *script* addresses, and a structure returned alongside it cannot answer that.
    """
    found: set[str] = set()
    for stage in chain:
        for token in stage.replace(";", ",").replace("[", ",").replace("]", ",").split(","):
            head = token.split("=", 1)[0].strip()
            if "@" in head:
                found.add(head)
    return found


# ------------------------------------------------------------------------------------------
# The pinned text.
# ------------------------------------------------------------------------------------------


def test_one_binding_compiles_to_this_exact_script():
    """The pin. Every number below was worked out from `ENVELOPE` by hand and written down.

    Temperature is the effect chosen because its one dial is spent on *two* filter options —
    `colorbalance` takes red up and blue down together — so the pin catches an option that went
    missing, an option that changed order, and a sign that flipped, none of which a single-option
    effect could show.

    The drive is `punch` at a floor of 0, so on this envelope: band 0 alone (the region is
    centred at 0 and narrow enough that band 1's weight is `exp(-70)`), a running average that
    starts at the first level and follows it at `0.03 * 60/4`, and a transient envelope that
    reaches 1 on both hits and releases by `0.88 ** (60/4)` per tick between them. Worked through
    with a pencil against those constants, the drive is
    `0, 1, 0.146974, 0.021601, 1, 0.146974, 0.021601, 0.003175`, and the resting 0.2 plus a depth
    of 0.6 is what is written below.
    """
    script = stages(
        [
            bound(
                "temperature",
                "amount",
                {"amount": 0.2},
                depth=0.6,
                band_centre=0.0,
                band_width=0.2,
            )
        ]
    ).scripts[0]

    assert script.target == "colorbalance@b0"
    assert script.filename == (
        "temperature-amount-b0-" + script.filename.split("-b0-")[1]
    )
    assert script.text == (
        "0 colorbalance@b0 rm 0.2;\n"
        "0 colorbalance@b0 bm -0.2;\n"
        "0.25 colorbalance@b0 rm 0.8;\n"
        "0.25 colorbalance@b0 bm -0.8;\n"
        "0.5 colorbalance@b0 rm 0.288184;\n"
        "0.5 colorbalance@b0 bm -0.288184;\n"
        "0.75 colorbalance@b0 rm 0.212961;\n"
        "0.75 colorbalance@b0 bm -0.212961;\n"
        "1 colorbalance@b0 rm 0.8;\n"
        "1 colorbalance@b0 bm -0.8;\n"
        "1.25 colorbalance@b0 rm 0.288184;\n"
        "1.25 colorbalance@b0 bm -0.288184;\n"
        "1.5 colorbalance@b0 rm 0.212961;\n"
        "1.5 colorbalance@b0 bm -0.212961;\n"
        "1.75 colorbalance@b0 rm 0.201905;\n"
        "1.75 colorbalance@b0 bm -0.201905;\n"
    )


def test_the_sustain_gate_compiles_to_this_exact_script_and_lets_go_slower_than_it_takes_hold():
    """The pin `sustain` did not have, and the property it carries is the one stated in the
    comment above `DRIVE_SUSTAIN_ATTACK_SECONDS`: *"Release is slower than attack for the same
    reason a fader is: a section that has arrived should not flicker out on one quiet bar."*

    **What a diff here means.** Every number below is the gate's own ramp, so a changed line is a
    changed attack or a changed release and nothing else: the band selection is the same one the
    punch pin uses (centred at 0 and narrow, so band 1's weight is `exp(-70)`), the parameter is
    Saturation at a resting 1.0 with a depth of 1.0, and the value written is therefore `1 + the
    gate`. Swapping the two constants — 0.35 attack and 0.7 release for 0.7 and 0.35 — rewrites
    lines 2 through 6 and nothing else in this repository notices, which is what this pin is for.

    Worked through with a pencil against `SUSTAIN_ENVELOPE` at a floor of 0.5, a hold of 0.5 s
    and a sustain of 0.25 s, one tick being 0.25 s: the band is above the floor from tick 0, so
    the gate has held for its half second by tick 1 and engages there. It then rises at
    `0.25 / 0.35` per tick — 0.714286, then the remaining 0.285714 — reaching full at tick 2. The
    band drops at tick 4, which spends the whole 0.25 s of sustain in one tick, so the gate lets
    go there and falls at `0.25 / 0.7` per tick — 0.357143 each — taking **three** ticks to reach
    nothing against the two it took to arrive. That asymmetry is the property, and it is asserted
    below on the numbers as well as pinned in the text, so a reader who changes the constants
    deliberately is told which of the two facts they have broken.
    """
    script = stages(
        [
            bound(
                "saturation",
                "amount",
                {"amount": 1.0},
                drive="sustain",
                depth=1.0,
                band_centre=0.0,
                band_width=0.2,
                floor=0.5,
                hold=0.5,
                sustain=0.25,
            )
        ],
        envelope=SUSTAIN_ENVELOPE,
    ).scripts[0]

    assert script.target == "eq@b0"
    assert script.text == (
        "0 eq@b0 saturation 1;\n"
        "0.25 eq@b0 saturation 1.714286;\n"
        "0.5 eq@b0 saturation 2;\n"
        "0.75 eq@b0 saturation 2;\n"
        "1 eq@b0 saturation 1.642857;\n"
        "1.25 eq@b0 saturation 1.285714;\n"
        "1.5 eq@b0 saturation 1;\n"
        "1.75 eq@b0 saturation 1;\n"
    )

    # The same property read off the numbers, in the fader's own terms: the fastest the gate ever
    # moves toward full is faster than the fastest it ever moves back toward nothing.
    #
    # **Read over every step rather than at two chosen lines**, because a swap of the constants
    # moves the ramps as well as their slopes: the first draft of this compared line 2 against
    # line 5 by index, and the swapped pair — which reaches full one tick later and lets go one
    # tick sooner — satisfied that comparison exactly as the correct pair does. The steepest
    # step in each direction is the one reading of the property that does not depend on where
    # the ramps happen to land.
    values = [float(line[:-1].split(" ")[3]) for line in script.text.splitlines()]
    steps = [later - earlier for earlier, later in itertools.pairwise(values)]
    assert max(steps) > -min(steps), steps


def test_the_compiler_is_a_function_of_an_envelope_a_binding_and_a_clips_seconds():
    """Called directly, with nothing but the three things AD-22 names.

    `build_effect_stages` is what every other test here goes through, because the target and the
    label have to come from one call. This one is the shape of the function itself: a Song
    Envelope, a Parameter Binding and the seconds a clip occupies, in — the script's text out,
    and nothing else consulted.
    """
    binding = ParameterBinding(
        effect_id="saturation",
        parameter="amount",
        drive="punch",
        depth=1.0,
        band_centre=0.0,
        band_width=0.2,
    )
    text = sendcmd_script(
        ENVELOPE,
        binding,
        target="eq@b0",
        resting=1.0,
        context=StageContext(width=EXPORT_WIDTH, height=EXPORT_HEIGHT),
        song_start=1.0,
        clip_seconds=0.5,
    )
    assert text == (
        "0 eq@b0 saturation 2;\n"
        "0.25 eq@b0 saturation 1.146974;\n"
    )
    # And nowhere else: the same call twice is the same characters, and the band it reads is the
    # one the binding names — moved to the quiet band it compiles a different, much smaller
    # drive from the same envelope and the same clip.
    assert text == sendcmd_script(
        ENVELOPE,
        binding,
        target="eq@b0",
        resting=1.0,
        context=StageContext(width=EXPORT_WIDTH, height=EXPORT_HEIGHT),
        song_start=1.0,
        clip_seconds=0.5,
    )
    elsewhere = sendcmd_script(
        ENVELOPE,
        dataclasses.replace(binding, band_centre=1.0),
        target="eq@b0",
        resting=1.0,
        context=StageContext(width=EXPORT_WIDTH, height=EXPORT_HEIGHT),
        song_start=1.0,
        clip_seconds=0.5,
    )
    assert elsewhere == (
        "0 eq@b0 saturation 1.390158;\n"
        "0.25 eq@b0 saturation 1.057343;\n"
    )


def test_the_same_inputs_compile_the_same_text_twice():
    """Determinism claimed about text, never about an mp4 (R-20). Nothing here has a clock."""
    stack = [bound("saturation", "amount", {"amount": 1.0})]
    assert stages(stack).scripts[0].text == stages(stack).scripts[0].text
    assert stages(stack).scripts[0].filename == stages(stack).scripts[0].filename


def test_the_script_is_written_in_sendcmds_own_grammar():
    """`START TARGET COMMAND ARG;` with the flags omitted, which is `[enter]` — its default, and
    what an instantaneous interval wants. Asserted as a shape so the pin above is not the only
    thing standing between this and a file ffmpeg parses as one long command."""
    text = stages([bound("contrast", "amount", {"amount": 1.0})]).scripts[0].text
    lines = text.splitlines()
    assert text.endswith("\n") and lines
    for line in lines:
        assert line.endswith(";")
        moment, target, command, argument = line[:-1].split(" ")
        assert float(moment) >= 0.0
        assert target == "eq@b0"
        assert command == "contrast"
        assert float(argument) >= 0.0


# ------------------------------------------------------------------------------------------
# Clip-local times: the constraint the artefacts do not address.
# ------------------------------------------------------------------------------------------


def test_a_shot_that_becomes_two_clips_compiles_each_clip_from_its_own_seconds():
    r"""The one I would most expect to be got wrong, and the reason it is checked three ways.

    `assembly.trim_args` prepends `setpts=PTS-STARTPTS` to every clip cut at an offset, so the
    filter graph's clock is zero at the first frame of **each** clip — and a Shot with another
    nested inside it *is* two clips, composed twice, keyed by clip index. A script whose times
    were the song's would drive the second clip with the first clip's seconds.

    So: each clip's script starts at time zero, the two are different text, and — the assertion
    that actually proves the derivation rather than merely its symptoms — **clip B's script is
    the tail of the whole-Shot script, re-timed**. Every line of B is a line the unsplit Shot
    also wrote, one second later, with the same value.

    Note what this could not catch if the drive were computed per clip instead of over the song:
    it would still start at zero and still differ, and only the third assertion would fail —
    which is why it is here.
    """
    stack = [bound("contrast", "amount", {"amount": 1.0}, depth=1.0, band_width=1.0)]
    whole = stages(stack, shot_start=0.0, clip_offset=0.0, clip_seconds=2.0).scripts[0].text
    first = stages(stack, shot_start=0.0, clip_offset=0.0, clip_seconds=1.0).scripts[0].text
    second = stages(stack, shot_start=0.0, clip_offset=1.0, clip_seconds=1.0).scripts[0].text

    assert first.startswith("0 eq@b0 contrast ")
    assert second.startswith("0 eq@b0 contrast ")
    assert first != second

    def timed(text: str, shift: float) -> list[tuple[float, str]]:
        return [
            (round(float(line.split(" ", 1)[0]) + shift, 6), line.split(" ", 1)[1])
            for line in text.splitlines()
        ]

    assert timed(first, 0.0) + timed(second, 1.0) == timed(whole, 0.0)


def test_the_song_second_a_clip_starts_at_is_the_shots_start_plus_the_clips_offset():
    """The same arithmetic `_compose_grain` reads to keep two clips off one noise sequence, read
    here for times instead of a seed. A Shot starting a second into the song and a clip starting
    a second into its Shot both land on the same song second, and compile the same script."""
    stack = [bound("contrast", "amount", {"amount": 1.0}, depth=1.0, band_width=1.0)]
    from_shot = stages(stack, shot_start=1.0, clip_offset=0.0, clip_seconds=1.0)
    from_clip = stages(stack, shot_start=0.0, clip_offset=1.0, clip_seconds=1.0)
    assert from_shot.scripts[0].text == from_clip.scripts[0].text
    # And a Shot that was never split adds nothing at all: it compiles from the song's own start.
    unsplit = stages(stack, shot_start=0.0, clip_offset=0.0, clip_seconds=1.0)
    assert unsplit.scripts[0].text != from_shot.scripts[0].text


def test_a_clip_that_starts_between_two_ticks_opens_on_the_tick_that_covers_it():
    """The rule `drive_samples` states and no fixture used to put it in the state where it holds:
    *"a first line at zero carrying the tick that covers `song_start`"*.

    **Every other binding fixture in this repository starts a Shot on an analysis-tick boundary**
    — 0.0, 0.5 and 1.0 here at 4 Hz, 0.0 and 4.0 at 30 Hz in the route and preview files — and on
    a boundary `floor` and `ceil` are the same function and `moment - song_start` is never
    negative. Both halves of the quantisation are then unguarded. A Director does not place Shots
    on ticks: a boundary dragged in the timeline lands wherever the pointer was.

    So this clip starts at 0.375 s, which at 4 Hz is half way between tick 1 and tick 2, and the
    two things that can go wrong there are pinned in one script:

    * **The opening tick.** Tick 1 is the tick that *covers* 0.375 s — it is the drive the clip's
      first frame is actually sitting in — so the first line is that tick, stamped at zero. Under
      `ceil` the walk starts at tick 2 instead, the clip begins at its resting value, and the hit
      at tick 1 is lost from a clip that is playing over it. The first line here carries 2, the
      full drive of that hit, and that is the whole of the difference.
    * **The negative timestamp.** Tick 1 sits 0.125 s *before* this clip's first frame, so
      `moment - song_start` is -0.125 and the clamp is the only thing between that and a line
      reading `-0.125` — which `sendcmd` rejects, taking the whole render with it rather than
      only the drive.

    Read `stages`' own numbers: the drive on `ENVELOPE` is `0, 1, 0.146974, 0.021601, 1, ...` and
    Saturation at a resting 1.0 with a depth of 1.0 writes `1 + the drive`.
    """
    binding = ParameterBinding(
        effect_id="saturation",
        parameter="amount",
        drive="punch",
        depth=1.0,
        band_centre=0.0,
        band_width=0.2,
    )
    text = sendcmd_script(
        ENVELOPE,
        binding,
        target="eq@b0",
        resting=1.0,
        context=StageContext(width=EXPORT_WIDTH, height=EXPORT_HEIGHT),
        song_start=0.375,
        clip_seconds=0.75,
    )
    assert text == (
        "0 eq@b0 saturation 2;\n"
        "0.125 eq@b0 saturation 1.146974;\n"
        "0.375 eq@b0 saturation 1.021601;\n"
        "0.625 eq@b0 saturation 2;\n"
    )
    # Said once more as the property rather than as the text, because these are the two lines a
    # reader of a diff needs to be able to name: no timestamp is negative, and the first one is
    # zero however far into a tick the clip begins.
    moments = [float(line.split(" ", 1)[0]) for line in text.splitlines()]
    assert moments[0] == 0.0
    assert min(moments) >= 0.0


def test_a_clip_that_outlives_the_analysis_holds_the_last_measured_value():
    """*"The last measured value held past the end of the analysis"* — `drive_samples`' own
    words, and until now nothing put a clip past that end.

    Every other fixture's song is at least twice its longest clip, so the tail branch is dead
    code in the whole suite. It is not dead in an export, and this is measured rather than
    assumed: an envelope holds `ceil(seconds * 30)` rows while the export's last clip ends on the
    24 fps cumulative grid at `round(seconds * 24) / 24`, which is up to half a video frame past
    the song itself. Swept over every song length from 1 s to 600 s at millisecond resolution,
    **61,018 of 599,001 — a bit over one length in ten — put the final clip's last tick past the
    final analysed row.** Which side of that a given project lands on is a rounding accident of
    its song's duration, so this is a fixture-shaped question and not a rare one.

    `TAIL_ENVELOPE` is one second long and its hit is on the last tick, so what happens in that
    fraction of a second is legible: the drive is **full** there. Held, the parameter stays at
    the top of its drive to the last frame. Falling to nothing instead, it snaps back to its
    resting value part-way through the clip's last quarter-second — which is a visible flinch on
    the final frames of a music video, and is what the pin below says out loud.
    """
    text = stages(
        [bound("contrast", "amount", {"amount": 1.0}, depth=1.0, band_centre=0.0,
               band_width=0.2)],
        envelope=TAIL_ENVELOPE,
        clip_seconds=1.5,
    ).scripts[0].text

    # Four analysed ticks (1 s), six compiled lines (1.5 s): the last two are past the analysis.
    assert text == (
        "0 eq@b0 contrast 1;\n"
        "0.25 eq@b0 contrast 1;\n"
        "0.5 eq@b0 contrast 1;\n"
        "0.75 eq@b0 contrast 2;\n"
        "1 eq@b0 contrast 2;\n"
        "1.25 eq@b0 contrast 2;\n"
    )


def test_two_clips_of_one_shot_are_two_files_even_in_one_directory():
    """The export writes every clip's intermediates into one directory. Named by effect and
    parameter alone the two scripts above would be one file, and whichever clip was written
    second would drive both — silently, because the wrong file is still a readable file."""
    stack = [bound("contrast", "amount", {"amount": 1.0}, depth=1.0, band_width=1.0)]
    first = stages(stack, clip_offset=0.0, clip_seconds=1.0).scripts[0]
    second = stages(stack, clip_offset=1.0, clip_seconds=1.0).scripts[0]
    assert first.filename != second.filename
    assert first.filename.startswith("contrast-amount-b0-")
    assert second.filename.startswith("contrast-amount-b0-")
    # Two clips that genuinely compile the same script share one file, which is right rather
    # than lucky: the name is a digest of the text.
    assert first.filename == stages(stack, clip_offset=0.0, clip_seconds=1.0).scripts[0].filename


def test_a_script_filename_needs_no_escaping_at_all():
    """AD-22 asks for a bare relative name, and the reason this one is safe is that every
    character in it comes from a catalogue id, a parameter name, a slot number or hex. No comma,
    no colon, no backslash, no `=` — the four things measured to break `sendcmd=f=`."""
    for effect_id, parameter in DRIVABLE:
        name = stages([bound(effect_id, parameter)]).scripts[0].filename
        assert set(name) <= set("abcdefghijklmnopqrstuvwxyz0123456789_-.")


# ------------------------------------------------------------------------------------------
# The target-label test. The single most load-bearing thing in this file.
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("effect_id", "parameter"), sorted(DRIVABLE))
def test_every_target_the_compiler_emits_is_a_label_in_the_chain_it_composed(
    effect_id: str, parameter: str
):
    """Nothing else can catch a typo here.

    Measured 2026-08-27 and reproduced below against the real binary: a command aimed at a target
    that does not exist is accepted, ignored, and reported nowhere — rc 0, silent at `-v
    warning`, frames byte-identical to the undriven chain. So the target and the label are
    checked against each other, from **one** call, for every parameter the music can reach.
    """
    composed = stages([bound(effect_id, parameter)])
    chain = (*composed.geometry, *composed.treatment)
    written = labels_in(chain)
    assert composed.scripts, f"{effect_id}.{parameter} composed no script"
    for script in composed.scripts:
        assert script.target in written, (script.target, chain)
        assert script.target == f"{DRIVABLE[(effect_id, parameter)][0]}@b0"


def test_the_target_carries_its_filter_class_because_the_bare_instance_reaches_nothing():
    """Measured 2026-08-27: `eq@b0` reaches a filter written `eq@b0=…`, and so does the class
    `eq`; the bare instance name `b0` reaches nothing at all. The class alone would be wrong for
    a different reason — a stack holding an Exposure, a Contrast and a Saturation composes three
    `eq` filters, and one command to `eq` would drive all three."""
    composed = stages(
        [
            bound("exposure", "amount"),
            bound("contrast", "amount", {"amount": 1.0}),
            bound("saturation", "amount", {"amount": 1.0}),
        ]
    )
    targets = [script.target for script in composed.scripts]
    assert targets == ["eq@b0", "eq@b1", "eq@b2"]
    assert len(set(targets)) == 3
    assert labels_in(composed.treatment) == {"eq@b0", "eq@b1", "eq@b2"}


def test_one_effect_bound_on_two_filters_labels_both_and_addresses_each_separately():
    """A Bloom is a branch, and its two drivable dials live in two different filters of it —
    intensity in the `blend` that rejoins, radius in the `gblur` inside the leg."""
    composed = stages(
        [
            {
                "effect": "bloom",
                "parameters": {"intensity": 0.5, "radius": 8.0},
                "bindings": [
                    {"parameter": "intensity", "drive": "punch", "depth": 0.3},
                    {"parameter": "radius", "drive": "punch", "depth": 8.0},
                ],
            }
        ]
    )
    assert sorted(script.target for script in composed.scripts) == ["blend@b0", "gblur@b0"]
    assert labels_in(composed.treatment) == {"blend@b0", "gblur@b0"}


def test_the_sendcmd_stage_sits_ahead_of_every_filter_it_drives():
    """`sendcmd` issues its commands while handling a frame and then passes that frame on, so a
    filter upstream of it would not see a new value until the frame after. It goes at the head of
    the chain — behind the branch frame guard only, which is the one stage that must see a frame
    still carrying the decoder's own duration and which changes no value at all."""
    composed = stages(
        [
            {"effect": "punch_in", "parameters": {"zoom": 1.2}},
            bound("saturation", "amount", {"amount": 1.0}),
        ]
    )
    # The geometry card is in the stack precisely so this is observable: with only treatments,
    # the geometry group holds nothing but the `sendcmd` stage and appending it after the effects
    # would produce the identical tuple. Composed at that one shape the assertion cannot fail.
    assert composed.geometry == (
        f"sendcmd=f={composed.scripts[0].filename}",
        "crop=w=iw/1.2:h=ih/1.2:x=(iw-ow)/2:y=(ih-oh)/2",
    )
    branched = stages(
        [
            {"effect": "punch_in", "parameters": {"zoom": 1.2}},
            bound("edge_treatment", "strength"),
        ]
    )
    assert branched.geometry == (
        "tpad=stop=1:stop_mode=clone",
        f"sendcmd=f={branched.scripts[0].filename}",
        "crop=w=iw/1.2:h=ih/1.2:x=(iw-ow)/2:y=(ih-oh)/2",
    )


# ------------------------------------------------------------------------------------------
# The drive table and the composers are one truth.
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("effect_id", "parameter"), sorted(DRIVABLE))
def test_the_value_a_command_carries_is_the_value_the_stage_was_composed_with(
    effect_id: str, parameter: str
):
    """`ParameterDrive.compute` is a second copy of arithmetic the composer also knows, and this
    is what makes the copy checkable rather than a second truth.

    The stage is composed at a value, the drive table is asked what that value is worth as a
    filter option, and every `option=value` it names must be present in the composed text. A
    composer that changed its option name, its sign or its scaling without the table following
    fails here rather than in a driven export nobody can read.
    """
    declared = next(
        entry
        for entry in EFFECT_CATALOGUE[effect_id].parameters
        if entry.name == parameter
    )
    assert isinstance(declared, NumberParameter)
    drive = declared.drive
    filter_name, options = DRIVABLE[(effect_id, parameter)]
    assert (drive.filter_name, drive.options) == (filter_name, options)

    # A value off the identity, so nothing here is testing the one arm where every arithmetic
    # agrees by accident.
    value = declared.minimum + (declared.maximum - declared.minimum) * 0.75
    if declared.integer:
        value = float(int(value))
    context = StageContext(width=EXPORT_WIDTH, height=EXPORT_HEIGHT, driven=True)
    values = dict.fromkeys(
        (entry.name for entry in EFFECT_CATALOGUE[effect_id].parameters), 0
    )
    for entry in EFFECT_CATALOGUE[effect_id].parameters:
        values[entry.name] = entry.default
    values[parameter] = value
    composed = " ".join(EFFECT_CATALOGUE[effect_id].compose(values, context))
    assert composed
    assert drive.compute is not None
    for option, argument in zip(options, drive.compute(value, context), strict=True):
        assert f"{option}={argument}" in composed, (option, argument, composed)


def test_the_catalogue_declares_a_drive_for_every_parameter_and_these_are_they():
    """The two tables at the top of this file against the catalogue, both ways.

    Written by hand so a parameter that silently lost its drive — becoming a bind glyph that
    refuses — or silently gained the wrong filter — becoming a binding that does nothing — is a
    failed test rather than a thing a Director discovers.
    """
    drivable: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}
    undrivable: set[tuple[str, str]] = set()
    for effect_id, definition in EFFECT_CATALOGUE.items():
        for parameter in definition.parameters:
            key = (effect_id, parameter.name)
            if isinstance(parameter, (ChoiceParameter, LutParameter)):
                undrivable.add(key)
            elif parameter.drive.drivable:
                drivable[key] = (parameter.drive.filter_name, parameter.drive.options)
            else:
                undrivable.add(key)
    assert drivable == DRIVABLE
    assert undrivable == set(UNDRIVABLE)
    assert len(drivable) + len(undrivable) == sum(
        len(definition.parameters) for definition in EFFECT_CATALOGUE.values()
    )


@pytest.mark.parametrize(("effect_id", "parameter"), sorted(UNDRIVABLE))
def test_a_parameter_the_music_cannot_reach_is_refused_by_name(effect_id: str, parameter: str):
    """Answerable from the compiler rather than discovered at render time — which it never would
    be, because there is nothing to discover. The sentence names the ffmpeg filter, because a
    Director who is told "no" is owed the reason and the reason is a fact about ffmpeg."""
    # A Grade names a look, and a *disabled* one is the only way to reach the binding check
    # without a looks folder: `_validate_lut` checks the folder for an enabled card, and an
    # effect's values are agreed before its bindings are. A disabled card's binding is still
    # validated, because a stack is stored whole and the card can be switched on at any moment.
    extra: dict[str, object] = (
        {"values": {"lut": "warm"}, "enabled": False} if effect_id == "lut_look" else {}
    )
    with pytest.raises(EffectRefusal) as refusal:
        stages([bound(effect_id, parameter, **extra)])  # type: ignore[arg-type]
    assert str(refusal.value) == UNDRIVABLE[(effect_id, parameter)]


# ------------------------------------------------------------------------------------------
# What a stack with no bindings still is.
# ------------------------------------------------------------------------------------------


def test_a_stack_with_no_bindings_composes_exactly_what_it_composes_today():
    """The AC that costs the most to break: *"a Shot with no bindings exports identically to one
    where the feature does not exist"*. Labelling every stage would have moved the composed chain
    for every project holding an effect — and the chain is the fourth input to
    `preview_fingerprint`, so it would have re-rendered every Shot on this machine for a picture
    that did not change."""
    stack = [
        {"effect": "punch_in", "parameters": {"zoom": 1.2}},
        {"effect": "grain", "parameters": {"strength": 12, "seed": 7}},
        {"effect": "temperature", "parameters": {"amount": 0.3}},
        {"effect": "bloom", "parameters": {"intensity": 0.4}},
    ]
    plain = build_effect_stages(stack, width=EXPORT_WIDTH, height=EXPORT_HEIGHT)
    assert plain.geometry == (
        "tpad=stop=1:stop_mode=clone",
        "crop=w=iw/1.2:h=ih/1.2:x=(iw-ow)/2:y=(ih-oh)/2",
    )
    assert plain.treatment == (
        "noise=alls=12:allf=t+u:all_seed=7",
        (
            "split=2[fx2a][fx2b];[fx2b]lutyuv=y=if(gt(val\\,169)\\,val\\,0):u=0:v=0,"
            "gblur=sigma=8[fx2c];[fx2a][fx2c]blend=all_mode=screen:all_opacity=0.4"
        ),
        "colorbalance=rm=0.3:bm=-0.3",
    )
    assert plain.scripts == ()
    # And handing it everything a binding would need changes nothing, because nothing is bound.
    assert (
        build_effect_stages(
            stack,
            width=EXPORT_WIDTH,
            height=EXPORT_HEIGHT,
            envelope=ENVELOPE,
            shot_start=3.0,
            clip_seconds=1.5,
        )
        == plain
    )


def test_a_bound_parameter_composes_its_stage_at_its_identity_value():
    """Ruled 2026-08-27. Every composer here but `mirror` and `lut_look` returns nothing at its
    identity, which is this module's oldest promise — so a parameter bound while sitting at rest
    would have had no filter instance for a command to address, and the binding would have been
    silently inert. Exactly the failure the whole slice is built to make impossible."""
    at_rest = stages([bound("temperature", "amount", {"amount": 0.0})])
    # `bm=0` rather than `bm=-0`: `_number` normalises negative zero, because two filter strings
    # for one number would be two fingerprints for one picture.
    assert at_rest.treatment == ("colorbalance@b0=rm=0:bm=0",)
    # Unbound, the same card composes nothing at all — the promise is unchanged for everyone
    # who has not asked for a binding.
    assert not build_effect_stages(
        [{"effect": "temperature", "parameters": {"amount": 0.0}}],
        width=EXPORT_WIDTH,
        height=EXPORT_HEIGHT,
    )


def test_the_whole_effect_composes_when_any_one_of_its_parameters_is_bound():
    """A Bloom whose radius is bound needs the whole branch — `blend` and `gblur` together —
    whatever its intensity says, so the flag is per effect and not per parameter."""
    composed = stages([bound("bloom", "radius", {"intensity": 0.0, "radius": 8.0}, depth=8.0)])
    assert composed.treatment and "gblur@b0=sigma=8" in composed.treatment[0]
    assert "blend=all_mode=screen:all_opacity=0" in composed.treatment[0]


# ------------------------------------------------------------------------------------------
# The envelope is read as it was taken.
# ------------------------------------------------------------------------------------------


def test_the_rate_and_the_band_count_come_off_the_envelope_and_not_from_constants():
    """`SongAnalysis` records both deliberately and its docstring says why: an envelope on disk
    was taken at whatever they were on the day it was written. Shipping on 8 bands is decided;
    assuming 8 bands is not — so the same music at half the rate compiles half as many lines, and
    a band selection means the same thing at either band count."""
    stack = [bound("contrast", "amount", {"amount": 1.0}, band_width=1.0)]
    at_four = stages(stack, clip_seconds=2.0).scripts[0].text
    slower = {
        **ENVELOPE,
        "analysis_rate": 2.0,
        "bands": [row[:4] for row in ENVELOPE["bands"]],  # type: ignore[index]
    }
    at_two = build_effect_stages(
        stack,
        width=EXPORT_WIDTH,
        height=EXPORT_HEIGHT,
        envelope=slower,
        clip_seconds=2.0,
    ).scripts[0].text
    assert len(at_four.splitlines()) == 8
    assert len(at_two.splitlines()) == 4
    # A four-band envelope is read as four bands, not as the eight this application analyses at.
    four_bands = {
        "analysis_rate": 4.0,
        "band_count": 4,
        "bands": [ENVELOPE["bands"][0], ENVELOPE["bands"][1]] * 2,  # type: ignore[index]
    }
    assert band_series(four_bands, centre=0.0, width=0.2, softness=0.0)[1] == pytest.approx(0.9)


def test_an_envelope_whose_recorded_band_count_disagrees_with_its_rows_is_not_read():
    """A file whose two halves disagree is not an envelope, and reporting the analysis **absent**
    is AD-21's discipline: a mismatch is never resolved by preferring one half."""
    broken = {**ENVELOPE, "band_count": 5}
    with pytest.raises(EffectRefusal) as refusal:
        build_effect_stages(
            [bound("contrast", "amount", {"amount": 1.0})],
            width=EXPORT_WIDTH,
            height=EXPORT_HEIGHT,
            envelope=broken,
            clip_seconds=1.0,
        )
    assert str(refusal.value) == (
        "contrast's amount is bound to the music, and this song has not been analysed. "
        "Analyse the song, or remove the binding. Nothing was composed."
    )


def test_a_binding_with_no_envelope_is_refused_rather_than_composed_inert():
    """FX-15, read the only way it can be read here. A binding is never silently dropped, and a
    chain that quietly forgot one would export a picture nobody asked for."""
    with pytest.raises(EffectRefusal) as refusal:
        build_effect_stages(
            [bound("contrast", "amount", {"amount": 1.0})],
            width=EXPORT_WIDTH,
            height=EXPORT_HEIGHT,
            clip_seconds=1.0,
        )
    assert "has not been analysed" in str(refusal.value)


def test_a_binding_with_no_clip_length_is_refused_by_name():
    with pytest.raises(EffectRefusal) as refusal:
        stages([bound("contrast", "amount", {"amount": 1.0})], clip_seconds=0.0)
    assert str(refusal.value) == (
        "contrast's amount is bound to the music, and no clip length was given to compile the "
        "drive against. Nothing was composed."
    )


# ------------------------------------------------------------------------------------------
# The drive model, ported rather than approximated.
# ------------------------------------------------------------------------------------------


def test_punch_flashes_on_hits_through_a_master_that_keeps_raw_level_pinned_high():
    """The property the mode exists for (FX-14), asserted rather than assumed.

    The signal is a limited master: level never falls below 0.8, and there are two hits. Raw
    level would sit near the top throughout and flash on nothing. Punch measures level *above its
    own running average*, so between the hits it falls back and on them it reaches full.
    """
    limited = [0.80, 0.82, 0.81, 0.80, 0.99, 0.83, 0.81, 0.80, 0.98, 0.82, 0.80, 0.80]
    punch = drive_series(
        limited, drive="punch", floor=0.0, hold=0.0, sustain=0.0, analysis_rate=4.0
    )
    assert min(limited) >= 0.8, "the fixture has to be a limited master or this proves nothing"
    assert punch[4] == pytest.approx(1.0)
    assert punch[8] > 0.8
    # And it is not pinned: between the hits it falls to nothing at all, on a signal whose raw
    # level never once drops below 0.8. Raw level as a drive would read 0.80..0.99 throughout.
    assert punch[3] == 0.0
    assert punch[7] < 0.01
    assert punch[11] < 0.01


def test_the_trigger_floor_silences_the_drive_rather_than_merely_lowering_it():
    """FX-14: *"below it the Drive contributes nothing"*. A silenced passage has to look
    silenced, which is also what the Drive readout draws."""
    levels = [0.1, 0.2, 0.1, 0.9, 0.2, 0.1]
    quiet = drive_series(
        levels, drive="punch", floor=0.5, hold=0.0, sustain=0.0, analysis_rate=4.0
    )
    assert [value for index, value in enumerate(quiet) if index != 3] == [0.0] * 5
    assert quiet[3] > 0.0


def test_sustain_engages_only_after_its_band_holds_and_survives_a_dip():
    """The section gate: *"engages only after its Band holds above a level for a hold time, and
    survives dips for a sustain time"*. A brief kiss of the threshold does nothing at all."""
    kiss = [0.0, 0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert drive_series(
        kiss, drive="sustain", floor=0.5, hold=1.0, sustain=1.0, analysis_rate=4.0
    ) == pytest.approx([0.0] * 8)

    # A hold of one second at 4 Hz is four ticks, so the gate is shut through tick 2 and opens
    # at tick 3 — and once open it rises rather than jumping, which is what a fader does.
    held = [0.9] * 6 + [0.0, 0.0] + [0.9] * 4
    gate = drive_series(
        held, drive="sustain", floor=0.5, hold=1.0, sustain=1.0, analysis_rate=4.0
    )
    assert gate[2] == 0.0
    assert 0.0 < gate[3] < 1.0
    assert gate[5] == pytest.approx(1.0)
    assert gate[7] == pytest.approx(1.0)  # the two-tick dip is survived, not dropped

    # A dip longer than the sustain time is a section that ended.
    ended = [0.9] * 6 + [0.0] * 8
    over = drive_series(
        ended, drive="sustain", floor=0.5, hold=1.0, sustain=1.0, analysis_rate=4.0
    )
    assert over[5] == pytest.approx(1.0)
    assert over[-1] == 0.0


def test_a_binding_never_drives_a_parameter_outside_its_own_declared_range():
    """FX-14's bound, held by arithmetic rather than only by the bound on depth. `contrast` runs
    0..3, and a resting value of 2.9 with a depth of 1 would reach 3.9 unclamped."""
    text = stages(
        [bound("contrast", "amount", {"amount": 2.9}, depth=1.0, band_width=1.0)]
    ).scripts[0].text
    values = [float(line[:-1].split(" ")[3]) for line in text.splitlines()]
    assert max(values) == 3.0
    assert min(values) >= 0.0
    # Negative depth pulls down, and stops at the floor rather than going under it.
    down = stages(
        [bound("contrast", "amount", {"amount": 0.1}, depth=-1.0, band_width=1.0)]
    ).scripts[0].text
    assert min(float(line[:-1].split(" ")[3]) for line in down.splitlines()) == 0.0


def test_a_depth_wider_than_the_parameter_it_drives_is_refused():
    with pytest.raises(EffectRefusal) as refusal:
        stages([bound("contrast", "amount", {"amount": 1.0}, depth=4.0)])
    assert str(refusal.value) == (
        "contrast's amount's depth is 4, above its maximum of 3. Nothing was composed."
    )


# ------------------------------------------------------------------------------------------
# The binding's own shape, guarded at the same boundary the stack is.
# ------------------------------------------------------------------------------------------


def test_the_three_decisions_a_binding_must_make_are_refused_when_missing():
    """Nothing infers a drive mode (FX-14), a binding that names no parameter drives nothing, and
    a depth of zero is a binding that silently does nothing — which is the failure this module
    refuses everywhere else."""
    with pytest.raises(EffectRefusal) as unnamed:
        stages([{"effect": "contrast", "bindings": [{"drive": "punch", "depth": 1.0}]}])
    assert str(unnamed.value) == (
        "A binding on contrast names no parameter. Every binding names the parameter it drives. "
        "Nothing was composed."
    )
    with pytest.raises(EffectRefusal) as undecided:
        stages([{"effect": "contrast", "bindings": [{"parameter": "amount", "depth": 1.0}]}])
    assert str(undecided.value) == (
        "contrast's amount's drive must be one of punch, sustain, and None is not. "
        "Nothing was composed."
    )
    with pytest.raises(EffectRefusal) as depthless:
        stages([{"effect": "contrast", "bindings": [{"parameter": "amount", "drive": "punch"}]}])
    assert str(depthless.value) == (
        "contrast's amount's depth must be a number, and None is not. Nothing was composed."
    )


def test_a_misspelled_binding_key_is_refused_rather_than_ignored():
    """The same rule one level down from `EFFECT_SPEC_KEYS`, and for the same reason: an ignored
    key is how a Director's Trigger Floor becomes a setting that quietly does nothing."""
    with pytest.raises(EffectRefusal) as refusal:
        stages(
            [
                {
                    "effect": "contrast",
                    "bindings": [
                        {"parameter": "amount", "drive": "punch", "depth": 1.0, "flor": 0.5}
                    ],
                }
            ]
        )
    assert str(refusal.value) == (
        "A binding on contrast has no key called 'flor'. It takes "
        + ", ".join(BINDING_SPEC_KEYS)
        + ". Nothing was composed."
    )


def test_a_parameter_carries_at_most_one_binding():
    """FX-12. Two bindings on one parameter is two answers to one question, and the second would
    have won by writing the same file twice."""
    with pytest.raises(EffectRefusal) as refusal:
        stages(
            [
                {
                    "effect": "contrast",
                    "bindings": [
                        {"parameter": "amount", "drive": "punch", "depth": 1.0},
                        {"parameter": "amount", "drive": "sustain", "depth": 0.5},
                    ],
                }
            ]
        )
    assert str(refusal.value) == (
        "contrast's amount carries two bindings, and a parameter may carry at most one. "
        "Nothing was composed."
    )


def test_a_binding_on_a_parameter_the_effect_does_not_have_is_refused_by_name():
    with pytest.raises(EffectRefusal) as refusal:
        stages([bound("contrast", "amont", {"amount": 1.0})])
    assert str(refusal.value) == (
        "contrast has no parameter called 'amont' to bind. It takes amount. "
        "Nothing was composed."
    )


def test_the_two_drives_are_the_only_two():
    assert DRIVE_MODES == ("punch", "sustain")
    with pytest.raises(EffectRefusal):
        stages([bound("contrast", "amount", {"amount": 1.0}, drive="ramp")])


def test_a_disabled_effect_composes_no_stage_and_no_script():
    """A switched-off card applies nothing, so there is nothing for the music to drive — but its
    binding is still validated, because a stack is stored whole and the card can be switched back
    on at any moment."""
    off = {
        "effect": "contrast",
        "enabled": False,
        "parameters": {"amount": 1.0},
        "bindings": [{"parameter": "amount", "drive": "punch", "depth": 1.0}],
    }
    assert not stages([off])
    assert stages([off]).scripts == ()
    with pytest.raises(EffectRefusal):
        stages([{**off, "bindings": [{"parameter": "amount", "drive": "punch", "depth": 9.0}]}])


# ------------------------------------------------------------------------------------------
# The two tests that run the real binary.
# ------------------------------------------------------------------------------------------


def ffmpeg(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ffmpeg", "-y", "-v", "warning", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=None if cwd is None else cwd.as_posix(),
    )


def source_clip(directory: Path) -> Path:
    """Two seconds of `testsrc2`, at the geometry the rest of this file composes against."""
    source = directory / "take.mp4"
    made = ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=2",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-pix_fmt", "yuv420p", source.as_posix(),
    )
    assert made.returncode == 0, made.stderr
    return source


def frames_of(rendered: Path) -> tuple[int, str]:
    """`(frame count, a digest of every frame)`, decoded rather than read off a header."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", rendered.as_posix(), "-f", "framemd5", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if not line.startswith("#")]
    return (len(lines), "\n".join(lines))


def test_a_compiled_script_drives_this_projects_own_ffmpeg(tmp_path: Path):
    """A pinned string proves the compiler is stable and proves nothing about whether ffmpeg
    accepts it. This is the other half.

    The chain is `assembly.trim_args`' own, built from a composed stack exactly as an export
    builds it, run with the process's working directory set to the script's — AD-22's remedy —
    and the result is compared against the *same* chain with the script's commands never issued.
    Different frames is the whole assertion: it is the only evidence that the commands landed,
    because a command that did not land costs rc 0 and says nothing.
    """
    source = source_clip(tmp_path)
    composed = build_effect_stages(
        [bound("saturation", "amount", {"amount": 1.0}, depth=2.0, band_width=1.0)],
        width=320,
        height=240,
        envelope=ENVELOPE,
        clip_seconds=2.0,
    )
    script = composed.scripts[0]
    (tmp_path / script.filename).write_text(script.text, encoding="utf-8", newline="\n")

    driven = tmp_path / "driven.mp4"
    result = ffmpeg(
        *trim_args(
            source, driven, 48, 320, 240,
            geometry_stages=composed.geometry,
            treatment_stages=composed.treatment,
        )[1:],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.strip() == ""

    # The same chain with the sendcmd stage removed: the filters are identical, the labels are
    # identical, and nothing ever issues a command.
    still = tmp_path / "still.mp4"
    quiet = ffmpeg(
        *trim_args(
            source, still, 48, 320, 240,
            geometry_stages=tuple(
                stage for stage in composed.geometry if not stage.startswith("sendcmd=")
            ),
            treatment_stages=composed.treatment,
        )[1:],
        cwd=tmp_path,
    )
    assert quiet.returncode == 0, quiet.stderr

    driven_count, driven_frames = frames_of(driven)
    still_count, still_frames = frames_of(still)
    assert driven_count == still_count == 48
    assert driven_frames != still_frames


def test_a_mistargeted_command_is_accepted_and_ignored(tmp_path: Path):
    """The measurement this whole slice is ordered around, reproduced so it stays true.

    A command aimed at a target that does not exist costs **rc 0**, says nothing even at
    `-v warning`, and renders frames byte-identical to the same chain undriven. There is no
    render-time evidence of a typo, on any screen, for any parameter, ever — which is why the
    target-label test above is the load-bearing thing in this file and not a nicety.
    """
    source = source_clip(tmp_path)
    (tmp_path / "wrong.cmds").write_text(
        "0 eq@nobody saturation 0;\n1 eq@nobody saturation 3;\n",
        encoding="utf-8",
        newline="\n",
    )
    chain = ("eq@b0=saturation=1",)
    aimed_wrong = tmp_path / "wrong.mp4"
    result = ffmpeg(
        *trim_args(
            source, aimed_wrong, 48, 320, 240,
            treatment_stages=("sendcmd=f=wrong.cmds", *chain),
        )[1:],
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert result.stderr.strip() == ""

    undriven = tmp_path / "undriven.mp4"
    plain = ffmpeg(
        *trim_args(source, undriven, 48, 320, 240, treatment_stages=chain)[1:],
        cwd=tmp_path,
    )
    assert plain.returncode == 0, plain.stderr
    assert frames_of(aimed_wrong) == frames_of(undriven)


# ------------------------------------------------------------------------------------------
# The Drive readout's numbers (story 10.3, R-27).
#
# The readout draws the compiled `sendcmd` values themselves rather than a curve derived a second
# way, and the form that guarantee takes here is not a test but a **shared walk**: `drive_samples`
# produces the numbers and `sendcmd_script` formats them into lines. So the assertions below are
# about the join — that rendering the readout's own samples through the filter's own option
# arithmetic reproduces the script character for character, and that the set of bindings the
# readout draws is exactly the set that compiles a script.
# ------------------------------------------------------------------------------------------


def rendered_from(readout, binding_values: dict[str, object], target: str) -> str:
    """The `sendcmd` text a readout's samples say ffmpeg will be handed.

    Written out here rather than borrowed from the compiler, deliberately: this is the only test
    in this file that is allowed to know the line format twice, because the claim being checked is
    that the *drawn series* and the *script text* are the same numbers. A helper that called the
    compiler to build the expectation would compare the compiler with itself.
    """
    parameter = next(
        declared
        for declared in EFFECT_CATALOGUE[readout.effect_id].parameters
        if declared.name == readout.parameter
    )
    context = StageContext(width=EXPORT_WIDTH, height=EXPORT_HEIGHT, slot=0,
                           driven=True, labels={parameter.drive.filter_name: "b0"})
    lines = []
    for sample in readout.samples:
        stamp = f"{sample.at:.6f}".rstrip("0").rstrip(".") or "0"
        for option, argument in zip(
            parameter.drive.options, parameter.drive.compute(sample.value, context), strict=True
        ):
            lines.append(f"{stamp} {target} {option} {argument};")
    return "\n".join(lines) + "\n"


def test_the_readout_draws_the_very_lines_the_script_carries():
    """R-27's whole claim, as one comparison: the drawn series **is** the argv.

    The same stack is put through `build_effect_stages` — the call the preview and the export both
    make — and through `drive_readout`, and the readout's samples are rendered back into
    `sendcmd`'s grammar. Byte for byte, that is the script the compiler wrote. There is no second
    engine here to drift, because the walk is one function; what this pins is that the numbers the
    browser is handed are the numbers on the lines and not something adjacent to them.
    """
    stack = [
        bound("temperature", "amount", {"amount": 0.2}, depth=0.6,
              band_centre=0.0, band_width=0.2)
    ]
    script = stages(stack).scripts[0]
    readouts = drive_readout(stack, envelope=ENVELOPE, clip_seconds=2.0)

    assert len(readouts) == 1
    assert rendered_from(readouts[0], stack[0]["bindings"][0], script.target) == script.text


def test_the_readout_names_the_card_and_the_parameter_it_belongs_to():
    """A Shot may carry more than one binding, so an unnamed envelope is a picture that lies about
    which parameter it describes. The identity travels with the numbers."""
    readouts = drive_readout(
        [bound("temperature", "amount", {"amount": 0.2})],
        envelope=ENVELOPE,
        clip_seconds=2.0,
    )

    assert (readouts[0].effect_id, readouts[0].parameter, readouts[0].index) == (
        "temperature", "amount", 0)


def test_the_readout_comes_back_in_the_order_the_scripts_are_written():
    """Composed-chain order, not stored order — the order ffmpeg is handed the scripts (AD-31).

    The stack below is stored Grade first and Texture second, which is the reverse of the order
    the chain composes them in. A readout list in stored order would name the wrong binding as
    *the first one the export drives*, which is the sentence the caption makes when no band panel
    is open.
    """
    stack = [
        bound("exposure", "amount", {"amount": 0.2}),
        bound("soft_focus", "sigma", {"sigma": 4.0}),
    ]
    readouts = drive_readout(stack, envelope=ENVELOPE, clip_seconds=2.0)
    compiled = stages(stack).scripts

    assert [(item.effect_id, item.index) for item in readouts] == [
        ("soft_focus", 1), ("exposure", 0)]
    # And the same order the scripts really came back in, so the two can never be read differently.
    assert [item.filename.split("-")[0] for item in compiled] == ["soft_focus", "exposure"]


def test_a_binding_on_a_switched_off_card_draws_nothing_because_it_drives_nothing():
    """A disabled card composes no stage and compiles no script (`test_a_disabled_effect_composes
    _no_stage_and_no_script`), so there is nothing for a readout to draw. Drawing one anyway would
    be a picture of a look the export will not produce, which is the one thing FX-22 forbids."""
    stack = [bound("temperature", "amount", {"amount": 0.2}, enabled=False)]

    assert stages(stack).scripts == ()
    assert drive_readout(stack, envelope=ENVELOPE, clip_seconds=2.0) == ()


def test_the_readout_marks_the_ticks_the_trigger_floor_shut():
    """*Silenced*, not merely low — the readout's whole reason for existing (FX-22).

    A floor of 0.5 over this envelope's low band shuts every tick but the two hits, and the flags
    are read off the **band level** rather than off the drive: a `punch` envelope decays to nothing
    two ticks after a hit in a passage the floor never touched, and calling that silenced would put
    the dim colour on the wrong seconds.
    """
    readout = drive_readout(
        [bound("temperature", "amount", {"amount": 0.2}, depth=0.6, band_centre=0.0,
               band_width=0.2, floor=0.5)],
        envelope=ENVELOPE,
        clip_seconds=2.0,
    )[0]

    # The low band is 0.1, 0.9, 0.2, 0.1, 0.8, 0.1, 0.3, 0.1 — two ticks above 0.5.
    assert [sample.silenced for sample in readout.samples] == [
        True, False, True, True, False, True, True, True]
    # And the flag is not "the drive is zero" wearing another name. Under `sustain` the same floor
    # over the same band leaves ticks that are **below the floor and still driving**, because the
    # gate holds through a dip — so a readout that inferred silence from the value would put the
    # dim colour on seconds the music is still moving.
    held = drive_readout(
        [bound("temperature", "amount", {"amount": 0.2}, drive="sustain", depth=0.6,
               band_centre=0.0, band_width=0.2, floor=0.5, hold=0.0, sustain=5.0)],
        envelope=ENVELOPE,
        clip_seconds=2.0,
    )[0]
    still_driving = [
        sample for sample in held.samples if sample.silenced and sample.value != held.rest
    ]
    assert still_driving, [dataclasses.astuple(sample) for sample in held.samples]


def test_the_readouts_two_ends_are_the_compilers_own_clamp_and_not_the_catalogues_bound():
    """`rest` and `reach` are where the parameter sits and where a full drive takes it, each
    already clamped — so a picture drawn between them is drawn between values the export can
    really produce. Exposure runs -1..1 and a resting 0.8 with a depth of 0.6 reaches 1, not 1.4.
    """
    readout = drive_readout(
        [bound("exposure", "amount", {"amount": 0.8}, depth=0.6)],
        envelope=ENVELOPE,
        clip_seconds=2.0,
    )[0]

    assert (readout.rest, readout.reach) == (0.8, 1.0)
    assert max(sample.value for sample in readout.samples) <= 1.0


def test_a_binding_that_pulls_its_parameter_down_reaches_below_where_it_rests():
    """Depth is signed, so `reach` below `rest` is an ordinary state and not an error."""
    readout = drive_readout(
        [bound("exposure", "amount", {"amount": 0.2}, depth=-0.5)],
        envelope=ENVELOPE,
        clip_seconds=2.0,
    )[0]

    assert readout.reach < readout.rest
    assert min(sample.value for sample in readout.samples) < readout.rest


def test_the_readout_refuses_without_an_envelope_in_the_compilers_own_words():
    """The same refusal `sendcmd_script` raises, because it is the same walk raising it."""
    with pytest.raises(EffectRefusal) as refused:
        drive_readout(
            [bound("temperature", "amount", {"amount": 0.2})],
            envelope=None,
            clip_seconds=2.0,
        )

    assert "temperature's amount" in str(refused.value)
