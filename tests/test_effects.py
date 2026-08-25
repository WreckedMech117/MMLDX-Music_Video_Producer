"""The effects chain, asserted the way this codebase asserts every generated render input.

Standing law 10: a filter chain is a pure function of the manifest and is compared **as text**.
So almost everything here is a string equality against a stage written out by hand — not derived
from the catalogue, because a test that computed its expectation from the same table the code
reads would pass just as happily for a table that had drifted.

Six tests run the real binary, and each is here because a string cannot prove what it claims:

* the generated `.cube` round-trips through `lut3d` at ~84 dB PSNR, and the same table written
  with the loops nested the wrong way round scores ~4 dB — the mistake that reports *nothing*;
  the same file is then loaded again from a directory whose name carries a space, a comma, a
  semicolon, brackets, a percent sign, an ampersand and an equals sign, which is the only
  evidence in this suite that the quoting rule survives anything but a drive-letter colon;
* a padded export carrying a texture leaves its letterbox bars at pure black, which is the one
  ordering constraint in this slice that is invisible in a still and wrong in a delivery;
* a pixelated white frame carries no black border, because a treatment may not resize a frame;
* every stage the catalogue can emit is accepted by this project's own ffmpeg, so a typo in a
  filter option is a failed test rather than a failed export — and each of those renders is
  counted back out with `ffprobe`, because `returncode == 0` is a syntax gate and the frame
  count is what the song is cut against;
* the whole catalogue stacked at once renders, which is the only real render of a combination;
* the same stack yields the same frames twice, and a different grain seed yields different ones.

**Nothing here derives an expectation from the catalogue.** Where a number had to be worked out
— a radian, a cosine, an inset, a film luma weight — it was worked out once by hand and written
down as a literal, because a test that recomputes the code's own arithmetic cannot catch a
misconception the two of them share.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from music_video_producer.assembly import trim_args
from music_video_producer.effects import (
    DEFAULT_LUT_SIZE,
    DEFAULT_LUTS,
    EFFECT_CATALOGUE,
    EFFECT_LUT_FILE_MISSING_REFUSAL,
    EFFECT_LUT_UNKNOWN_REFUSAL,
    FAMILY_GEOMETRY,
    FAMILY_GRADE,
    FAMILY_ORDER,
    FAMILY_STYLIZE,
    FAMILY_TEXTURE,
    PRE_PAD_FAMILIES,
    PRE_SCALE_FAMILIES,
    ChoiceParameter,
    EffectRefusal,
    EffectStages,
    LutEntry,
    LutParameter,
    NumberParameter,
    build_effect_stages,
    cube_text,
    discover_luts,
    identity_transform,
    lut_directory,
    lut_file_argument,
    lut_id_for_name,
    validate_stack,
    write_default_luts,
)

EXPORT_WIDTH = 1056
EXPORT_HEIGHT = 608


def effect(effect_id: str, /, enabled: bool = True, **parameters: object) -> dict[str, object]:
    """One stack entry in the plain shape slice C's `EffectSpec` will serialise to."""
    return {"effect": effect_id, "parameters": parameters, "enabled": enabled}


def stages(stack: list[dict[str, object]], **kwargs: object) -> EffectStages:
    return build_effect_stages(
        stack, width=EXPORT_WIDTH, height=EXPORT_HEIGHT, **kwargs  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------------------------------
# The fixed order, and the sort that makes storage order stop mattering.
# ------------------------------------------------------------------------------------------


def test_the_stage_order_is_the_one_ad_17_fixed():
    """Pinned as data, not as a consequence. Changing this tuple is an Ask First, and this is
    the test that says so out loud."""
    assert FAMILY_ORDER == (FAMILY_GEOMETRY, FAMILY_TEXTURE, FAMILY_GRADE, FAMILY_STYLIZE)
    assert PRE_SCALE_FAMILIES == (FAMILY_GEOMETRY,)
    assert PRE_PAD_FAMILIES == (FAMILY_TEXTURE, FAMILY_GRADE, FAMILY_STYLIZE)
    # No family is placed twice. The companion claim — that every family in `FAMILY_ORDER` is
    # placed *somewhere* — is a module-level `assert` in `effects.py` that runs at import, so
    # repeating it here could never fail: a false one would stop this module importing at all.
    # It is left where it is, and this test pins the three tuples themselves instead.
    assert set(PRE_SCALE_FAMILIES).isdisjoint(PRE_PAD_FAMILIES)
    assert {definition.family for definition in EFFECT_CATALOGUE.values()} == set(FAMILY_ORDER)


def test_an_empty_stack_composes_nothing_at_all():
    """The first row of the matrix, and the one every existing export depends on."""
    built = stages([])
    assert built == EffectStages(geometry=(), treatment=())
    assert not built


def test_one_effect_per_family_lands_in_the_fixed_order():
    """Geometry alone before `scale`; texture, then grade, then stylize before `pad`."""
    built = stages(
        [
            effect("punch_in", zoom=1.5),
            effect("grain", strength=12),
            effect("saturation", amount=1.4),
            effect("posterize", levels=8),
        ]
    )
    assert built.geometry == ("crop=w=iw/1.5:h=ih/1.5:x=(iw-ow)/2:y=(ih-oh)/2",)
    assert built.treatment == (
        "noise=alls=12:allf=t+u:all_seed=0",
        "eq=saturation=1.4",
        "lutyuv=y=trunc(val/32)*32",
    )


def test_a_stack_stored_out_of_family_order_composes_in_the_fixed_order_anyway():
    """AD-31, and `BUILD-ORDER.md` calls this the difference between a copied stack behaving
    and a copied stack quietly rendering differently.

    The stored order below is deliberately the reverse of the legal one — stylize, grade,
    texture, geometry — which is exactly what a hand-edited manifest or an older client can
    produce. The composed chain is identical to the one the legal order produces, asserted
    whole rather than by inspecting positions.
    """
    legal = [
        effect("punch_in", zoom=1.25),
        effect("vignette", angle=0.6),
        effect("contrast", amount=1.2),
        effect("chroma_split", shift=0.005),
    ]
    scrambled = list(reversed(legal))
    assert stages(scrambled) == stages(legal)
    assert stages(scrambled) == EffectStages(
        geometry=("crop=w=iw/1.25:h=ih/1.25:x=(iw-ow)/2:y=(ih-oh)/2",),
        treatment=(
            "vignette=angle=0.6",
            "eq=contrast=1.2",
            "chromashift=cbh=5:crh=-5",
        ),
    )


def test_two_effects_of_one_family_keep_the_directors_order_between_them():
    """Family order is fixed; order *within* a family is the Director's, and the sort is stable.

    Two textures whose stored order is swapped must produce two different chains — otherwise
    "the Director's order is preserved" would be a claim with no evidence behind it.
    """
    grain_first = stages([effect("grain", strength=8), effect("soft_focus", sigma=2)])
    blur_first = stages([effect("soft_focus", sigma=2), effect("grain", strength=8)])
    assert grain_first.treatment == ("noise=alls=8:allf=t+u:all_seed=0", "gblur=sigma=2")
    assert blur_first.treatment == ("gblur=sigma=2", "noise=alls=8:allf=t+u:all_seed=0")
    assert grain_first != blur_first

    # And a geometry effect wedged between them does not disturb their relative order.
    interleaved = stages(
        [effect("grain", strength=8), effect("punch_in", zoom=1.1), effect("soft_focus", sigma=2)]
    )
    assert interleaved.treatment == grain_first.treatment


def test_an_effect_may_compose_to_more_than_one_stage_and_they_stay_together():
    """Lift/gamma/gain is two filters because ffmpeg has no one filter for it. The pair is
    emitted adjacent and in a fixed internal order, so the two halves of one control can never
    be separated by another effect."""
    built = stages([effect("lift_gamma_gain", lift=0.1, gamma=1.2, gain=-0.05)])
    assert built.treatment == (
        "colorbalance=rs=0.1:gs=0.1:bs=0.1:rh=-0.05:gh=-0.05:bh=-0.05",
        "eq=gamma=1.2",
    )


def test_a_disabled_effect_is_validated_and_not_composed():
    """A disabled card is retained, not deleted — so its values are still checked, because it
    can be switched back on between now and the export."""
    assert stages([effect("grain", enabled=False, strength=30)]) == EffectStages()
    with pytest.raises(EffectRefusal) as refusal:
        stages([effect("grain", enabled=False, strength=900)])
    assert "grain's strength is 900" in str(refusal.value)


def test_a_switched_off_grade_does_not_brick_an_export_over_a_deleted_file(tmp_path: Path):
    """The line between the two halves of that rule: a *spec* is checked whether the card is on
    or off, and the **folder** is not.

    `build_effect_stages` always skipped the file-existence check for a disabled effect, and the
    id-existence check one function earlier did not — so deleting one `.cube` refused every
    export of every project holding a switched-off card that named it, a grade the Director can
    see is off. The two are now the same tolerance.
    """
    write_default_luts(lut_directory(tmp_path), size=5)
    luts = discover_luts(tmp_path)

    # An id that no longer exists, on a card that is off: composed as nothing, not refused.
    assert stages([effect("lut_look", enabled=False, lut="deleted-look")], luts=luts) == (
        EffectStages()
    )
    # The same card switched on is still refused by name.
    with pytest.raises(EffectRefusal) as switched_on:
        stages([effect("lut_look", lut="deleted-look")], luts=luts)
    assert str(switched_on.value) == EFFECT_LUT_UNKNOWN_REFUSAL.format(lut="deleted-look")
    # And a file that has gone since discovery, on a card that is off, is the same tolerance.
    warm = next(entry for entry in luts if entry.lut_id == "warm-shift")
    warm.path.unlink()
    assert stages([effect("lut_look", enabled=False, lut="warm-shift")], luts=luts) == (
        EffectStages()
    )
    # What is *not* tolerated is a spec that is wrong on its own terms. Those are the stack's
    # business rather than the folder's, and a disabled card is still refused for them.
    with pytest.raises(EffectRefusal) as unnamed:
        stages([effect("lut_look", enabled=False)], luts=luts)
    assert "lut_look needs a look chosen" in str(unnamed.value)
    with pytest.raises(EffectRefusal) as bad_choice:
        stages([effect("lut_look", enabled=False, lut="warm-shift", interp="cubic")], luts=luts)
    assert "lut_look's interp must be one of" in str(bad_choice.value)


# ------------------------------------------------------------------------------------------
# The splice into `trim_args`.
# ------------------------------------------------------------------------------------------


def test_the_splice_puts_geometry_before_scale_and_treatments_before_pad():
    """The two insertion points, asserted on the argv itself rather than on the stage groups.

    `pad` onward is untouched: the rate, the SAR and the pixel format still close the chain in
    the order every existing intermediate was built with, which is what keeps concat working.
    """
    built = stages(
        [effect("punch_in", zoom=1.5), effect("grain", strength=10), effect("monochrome")]
    )
    args = trim_args(
        Path("in.mp4"),
        Path("out.mp4"),
        frames=90,
        width=EXPORT_WIDTH,
        height=EXPORT_HEIGHT,
        offset=0.25,
        geometry_stages=built.geometry,
        treatment_stages=built.treatment,
    )
    assert args[args.index("-vf") + 1] == (
        "trim=start_frame=6,setpts=PTS-STARTPTS,"
        "crop=w=iw/1.5:h=ih/1.5:x=(iw-ow)/2:y=(ih-oh)/2,"
        "scale=1056:608:force_original_aspect_ratio=decrease,"
        "noise=alls=10:allf=t+u:all_seed=0,"
        "hue=s=0,"
        "pad=1056:608:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p"
    )


def test_a_shot_with_no_effects_builds_exactly_what_this_application_builds_today():
    """The matrix's first row, at the argv. Written out rather than compared against a call
    with the arguments omitted, so a default that changed would still be caught."""
    empty = stages([])
    with_empty_groups = trim_args(
        Path("in.mp4"),
        Path("out.mp4"),
        frames=90,
        width=EXPORT_WIDTH,
        height=EXPORT_HEIGHT,
        geometry_stages=empty.geometry,
        treatment_stages=empty.treatment,
    )
    assert with_empty_groups == [
        "ffmpeg", "-y", "-v", "error", "-i", "in.mp4",
        "-vf",
        (
            "scale=1056:608:force_original_aspect_ratio=decrease,"
            "pad=1056:608:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p"
        ),
        "-frames:v", "90", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "out.mp4",
    ]
    assert with_empty_groups == trim_args(
        Path("in.mp4"), Path("out.mp4"), frames=90, width=EXPORT_WIDTH, height=EXPORT_HEIGHT
    )


def test_geometry_precedes_scale_so_a_punch_in_samples_the_takes_own_pixels():
    """The constraint that is invisible in a still and obvious in motion, asserted as position.

    A `crop` after `scale` would be cropping a frame that had already been resampled to the
    export grid — the punch would be a blow-up of an interpolation rather than of the take.
    """
    built = stages([effect("punch_in", zoom=1.4)])
    chain = trim_args(
        Path("in.mp4"),
        Path("out.mp4"),
        frames=90,
        width=EXPORT_WIDTH,
        height=EXPORT_HEIGHT,
        geometry_stages=built.geometry,
        treatment_stages=built.treatment,
    )
    filters = chain[chain.index("-vf") + 1].split(",")
    assert filters.index("crop=w=iw/1.4:h=ih/1.4:x=(iw-ow)/2:y=(ih-oh)/2") < filters.index(
        "scale=1056:608:force_original_aspect_ratio=decrease"
    )


def test_every_treatment_precedes_pad():
    """The other measured constraint, as position. The pixels are asserted further down, by
    running the real thing — this half is the cheap guard that catches a reordering edit."""
    built = stages(
        [
            effect("grain", strength=20),
            effect("vignette", angle=0.8),
            effect("monochrome"),
            effect("posterize", levels=6),
        ]
    )
    chain = trim_args(
        Path("in.mp4"),
        Path("out.mp4"),
        frames=90,
        width=EXPORT_WIDTH,
        height=EXPORT_HEIGHT,
        treatment_stages=built.treatment,
    )
    filters = chain[chain.index("-vf") + 1].split(",")
    pad = filters.index("pad=1056:608:(ow-iw)/2:(oh-ih)/2")
    for stage in built.treatment:
        assert filters.index(stage) < pad


# ------------------------------------------------------------------------------------------
# Geometry is composed against the export's dimensions, never the take's.
# ------------------------------------------------------------------------------------------


def test_a_treatment_measured_in_pixels_is_composed_against_the_exports_geometry():
    """Chroma split is stored as a fraction and turned into pixels against the export's width,
    because that stage runs *after* `scale` and the frame is the export's size by then. The same
    stored look therefore ships the same split at any delivery size."""
    stack = [effect("chroma_split", shift=0.01)]
    wide = build_effect_stages(stack, width=1920, height=1080)
    small = build_effect_stages(stack, width=640, height=360)
    assert wide.treatment == ("chromashift=cbh=19:crh=-19",)
    assert small.treatment == ("chromashift=cbh=6:crh=-6",)


def test_geometry_addresses_the_takes_pixels_through_ffmpegs_own_expressions():
    """The mirror image of the test above. A geometry stage runs *before* `scale`, so it must
    not carry an export number at all — it addresses whatever the take happens to be through
    `iw`/`ih`, and the same stage text comes out for any export size."""
    stack = [
        effect("punch_in", zoom=1.3),
        effect("dutch_tilt", angle=6),
        effect("handheld_shake", amplitude=0.02, frequency=3),
    ]
    wide = build_effect_stages(stack, width=1920, height=1080)
    small = build_effect_stages(stack, width=640, height=360)
    assert wide.geometry == small.geometry
    for stage in wide.geometry:
        assert "1920" not in stage and "1080" not in stage


def test_a_dutch_tilt_crops_back_inside_the_frame_it_rotated():
    """A rotation fills the corners with black. The crop that follows is what keeps the tilt from
    exposing an undefined edge, and its factor is an expression over `iw`/`ih` because this stage
    runs before `scale` and has no idea what shape the take is.

    Written out as literals rather than rebuilt from `math.radians` and a reimplementation of
    the composer's own float formatter. A mirror implementation cannot catch a misconception the
    test and the code share — degrees where radians belong, a sine where a cosine belongs — so
    the numbers below were computed once, by hand, and are now the contract: 10 degrees is
    0.174533 radians, its cosine 0.984808 and its sine 0.173648, each to six decimals.
    """
    assert stages([effect("dutch_tilt", angle=10)]).geometry == (
        "rotate=a=0.174533:ow=iw:oh=ih",
        (
            "crop=w=iw/max((iw*0.984808+ih*0.173648)/iw\\,(iw*0.173648+ih*0.984808)/ih)"
            ":h=ih/max((iw*0.984808+ih*0.173648)/iw\\,(iw*0.173648+ih*0.984808)/ih)"
            ":x=(iw-ow)/2:y=(ih-oh)/2"
        ),
    )
    # Tilted the other way, the rotation is negative and the *crop* is identical: the inscribed
    # rectangle is the same shape either way, which is why the composer takes the magnitude of
    # both trig terms. A signed sine here would write a negative width into the `max()`.
    assert stages([effect("dutch_tilt", angle=-10)]).geometry == (
        "rotate=a=-0.174533:ow=iw:oh=ih",
        (
            "crop=w=iw/max((iw*0.984808+ih*0.173648)/iw\\,(iw*0.173648+ih*0.984808)/ih)"
            ":h=ih/max((iw*0.984808+ih*0.173648)/iw\\,(iw*0.173648+ih*0.984808)/ih)"
            ":x=(iw-ow)/2:y=(ih-oh)/2"
        ),
    )
    # At zero there is no pair at all. It used to emit `rotate=a=0` and a crop by a factor of 1,
    # which reproduce their own input exactly — measured `inf` PSNR — for the price of two real
    # filters on every frame of the shot.
    assert stages([effect("dutch_tilt")]).geometry == ()
    assert stages([effect("dutch_tilt", angle=0)]).geometry == ()


# ------------------------------------------------------------------------------------------
# Every composer's filter text, written out by hand.
#
# The acceptance sweep at the bottom of this file runs all twenty through the real binary and
# asserts `returncode == 0`. That is a *syntax* gate: a filter that is well-formed and wrong
# passes it, and a wrong-but-well-formed filter is exactly what a slipped option produces. So
# each composer below is pinned as a string, at a value off its identity, with every number
# computed by hand rather than taken from the catalogue or recomputed with the composer's own
# formatter.
# ------------------------------------------------------------------------------------------


def test_a_handheld_shake_insets_its_window_by_the_amplitude_on_all_four_sides():
    """The bound FX-11 states — *"geometry that would sample outside the source frame is
    bounded so it cannot expose an undefined edge"* — and the one that fails silently.

    The window is `1 - 2*amplitude` of the frame, so an offset of `amplitude` in either
    direction on either axis still lands inside the source. Take the inset away and the window
    is the whole frame: ffmpeg then clamps the moving crop back to the frame's own edge on
    every frame, the offset has nowhere to go, and **the shake stops shaking** — no error, no
    warning, an effect that renders as its own input. Nothing else in this file would notice.

    The vertical frequency is the horizontal one times 1.37, so the two axes do not return to
    the same place together and the motion does not read as a circle. Both numbers below were
    worked out by hand: 1 - 2*0.03 = 0.94, and 3.5 * 1.37 = 4.795.
    """
    assert stages([effect("handheld_shake", amplitude=0.03, frequency=3.5)]).geometry == (
        (
            "crop=w=iw*0.94:h=ih*0.94"
            ":x=(iw-ow)/2+iw*0.03*sin(2*PI*3.5*t)"
            ":y=(ih-oh)/2+ih*0.03*cos(2*PI*4.795*t)"
        ),
    )
    # At the amplitude's own maximum the inset is at its largest: 1 - 2*0.05 = 0.9, a window
    # nine tenths of the frame, and 10 * 1.37 = 13.7 on the vertical.
    assert stages([effect("handheld_shake", amplitude=0.05, frequency=10)]).geometry == (
        (
            "crop=w=iw*0.9:h=ih*0.9"
            ":x=(iw-ow)/2+iw*0.05*sin(2*PI*10*t)"
            ":y=(ih-oh)/2+ih*0.05*cos(2*PI*13.7*t)"
        ),
    )


def test_a_mirror_writes_the_flip_its_axis_names_on_every_axis():
    """Three axes, three answers, and only the default was pinned. `vertical` emitting `hflip`
    is a mirror that mirrors the wrong way — perfectly legal ffmpeg, and wrong in the picture."""
    assert stages([effect("mirror", axis="horizontal")]).geometry == ("hflip",)
    assert stages([effect("mirror", axis="vertical")]).geometry == ("vflip",)
    assert stages([effect("mirror", axis="both")]).geometry == ("hflip", "vflip")


def test_the_texture_composers_write_the_options_they_mean():
    """Sharpen's radius, and deband's four planes.

    `unsharp`'s matrix size is a radius: 5 and 7 are both valid and produce visibly different
    pictures, so a slipped digit is a silently different effect. `deband` carries a threshold
    per plane and the catalogue offers one dial, so all four must carry it — three of the four
    left at the filter's own default would deband the luma and leave the chroma banded, which
    is the artefact the card exists to remove.
    """
    assert stages([effect("sharpen", amount=1.25)]).treatment == (
        "unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount=1.25",
    )
    # The range crosses zero: a negative amount softens, and the sign reaches the filter.
    assert stages([effect("sharpen", amount=-0.5)]).treatment == (
        "unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount=-0.5",
    )
    assert stages([effect("banding_suppression", threshold=0.02)]).treatment == (
        "deband=1thr=0.02:2thr=0.02:3thr=0.02:4thr=0.02",
    )


def test_the_grade_composers_write_the_axis_they_mean():
    """Exposure is brightness, temperature is red against blue, tint is green against magenta.

    Each of these is one `eq` or `colorbalance` option away from being a different control
    entirely, and every one of those neighbours is a valid option name. Exposure emitting
    `eq=contrast=` is an Exposure slider that changes contrast; temperature with `rm` and `bm`
    exchanged makes warm cool; tint on `gs` instead of `gm` casts the blacks green instead of
    the midtones. All three ship green through a syntax gate.
    """
    assert stages([effect("exposure", amount=0.2)]).treatment == ("eq=brightness=0.2",)
    assert stages([effect("exposure", amount=-0.35)]).treatment == ("eq=brightness=-0.35",)

    # One dial, two options, opposite signs — and the sign is which way "warm" points.
    assert stages([effect("temperature", amount=0.4)]).treatment == (
        "colorbalance=rm=0.4:bm=-0.4",
    )
    assert stages([effect("temperature", amount=-0.4)]).treatment == (
        "colorbalance=rm=-0.4:bm=0.4",
    )

    # `gm` is the midtones. `gs` is the shadows, and the module docstring's whole reason for
    # choosing the midtone options is that a grade must not put a cast in the black point.
    assert stages([effect("tint", amount=0.3)]).treatment == ("colorbalance=gm=0.3",)
    assert stages([effect("tint", amount=-0.25)]).treatment == ("colorbalance=gm=-0.25",)


def test_pixelate_quantises_in_place_and_says_which_mode():
    """`pixelize` with the averaging mode written out.

    The mode is stated rather than left to the filter's default so the stage text is this
    application's decision — and because the neighbouring modes are not pixelation: `min` and
    `max` are morphological, and either would read as a smear rather than as blocks.
    """
    assert stages([effect("pixelate", size=8)]).treatment == ("pixelize=w=8:h=8:mode=avg",)
    assert stages([effect("pixelate", size=64)]).treatment == ("pixelize=w=64:h=64:mode=avg",)


def test_a_chroma_split_rounds_to_the_nearest_pixel_rather_than_truncating():
    """Half a pixel is the only place `round` and `int` disagree, so it is the only place the
    difference can be pinned.

    0.005 of 1900 is 9.5 exactly. Rounded that is 10; truncated it is 9 — a whole pixel of
    chroma offset, on a control whose entire range is 40 pixels wide at that width. The two
    fixtures elsewhere in this file (19.2 at 1920, 6.4 at 640) truncate to the same number they
    round to, which is why neither of them says anything about this.
    """
    stack = [effect("chroma_split", shift=0.005)]
    assert build_effect_stages(stack, width=1900, height=1068).treatment == (
        "chromashift=cbh=10:crh=-10",
    )
    # And on the negative half of the range, where truncation moves the other way.
    away = [effect("chroma_split", shift=-0.005)]
    assert build_effect_stages(away, width=1900, height=1068).treatment == (
        "chromashift=cbh=-10:crh=10",
    )


# ------------------------------------------------------------------------------------------
# Defaults, and the refusals. Every one names its offender.
# ------------------------------------------------------------------------------------------


def test_an_omitted_parameter_takes_the_catalogues_default():
    """Read off the *composed stage*, so it is the value that reached the filter that is asserted
    and not the value the validator wrote down.

    Every expectation is written out here rather than derived from the catalogue, so a default
    that drifted would fail this test rather than redefine it. The effects below are set off
    their identity by one parameter and left alone on the others, because an effect sitting at
    every identity value at once composes to nothing at all — which is the next test.
    """
    # `seed` omitted: the grain still carries one, and it is 0.
    assert stages([effect("grain", strength=8)]).treatment == (
        "noise=alls=8:allf=t+u:all_seed=0",
    )
    # `gamma` and `gain` omitted: both are filled in, and the pair is still two stages.
    assert stages([effect("lift_gamma_gain", lift=0.1)]).treatment == (
        "colorbalance=rs=0.1:gs=0.1:bs=0.1:rh=0:gh=0:bh=0",
        "eq=gamma=1",
    )
    # `interp` omitted, and its default is a word rather than a number.
    assert stages([effect("mirror")]).geometry == ("hflip",)
    assert stages([effect("handheld_shake", amplitude=0.02)]).geometry == (
        (
            "crop=w=iw*0.96:h=ih*0.96"
            ":x=(iw-ow)/2+iw*0.02*sin(2*PI*2*t)"
            ":y=(ih-oh)/2+ih*0.02*cos(2*PI*2.74*t)"
        ),
    )
    # A spec carrying no `parameters` key at all is the same thing as one carrying an empty map.
    assert stages([{"effect": "grain"}]) == stages([effect("grain")])


def test_an_effect_at_its_identity_values_composes_no_stage_at_all():
    """The Spec Change Log's claim, made true rather than softened: *"every other parameter in
    the catalogue defaults to a value that changes no pixel."*

    A filter that does no arithmetic is not the same as no filter. `colorbalance=rm=0:bm=0`
    computes nothing and still drags the frame through `yuv420p -> gbrp -> yuv420p`, which
    measured 47.10 dB average PSNR against the same chain without it; `lutyuv` at a step of 1
    leaves luma at `inf` and takes chroma through 4:4:4 at u:59.81 v:63.96. So a card at its
    identity emits nothing, and the claim holds at the pixel rather than in the arithmetic.

    `mirror` and `monochrome` are the two the Change Log names as having no identity *default* —
    adding either one is the request — and they are the two that still compose at their defaults.
    """
    for effect_id in (
        "punch_in",
        "handheld_shake",
        "dutch_tilt",
        "grain",
        "vignette",
        "soft_focus",
        "sharpen",
        "banding_suppression",
        "exposure",
        "contrast",
        "saturation",
        "temperature",
        "tint",
        "lift_gamma_gain",
        "chroma_split",
        "posterize",
        "pixelate",
    ):
        assert stages([{"effect": effect_id}]) == EffectStages(), effect_id

    assert stages([effect("mirror")]).geometry == ("hflip",)
    assert stages([effect("monochrome")]).treatment == ("hue=s=0",)

    # An identity reached explicitly is the same as an identity left alone, and an identity value
    # that is not the default counts too: monochrome at 0 is `hue=s=1`, which reproduces its own
    # input and charges a filter pass for it.
    assert stages([effect("temperature", amount=0)]) == EffectStages()
    assert stages([effect("monochrome", amount=0)]) == EffectStages()
    assert stages([effect("posterize", levels=256)]) == EffectStages()
    assert stages([effect("pixelate", size=1)]) == EffectStages()

    # A shift too small to move a whole pixel at this width is a shift of none. The identity is
    # decided on what the filter would be handed, not on the stored fraction.
    assert stages([effect("chroma_split", shift=0.0004)]) == EffectStages()
    assert stages([effect("chroma_split", shift=0.0005)]).treatment == (
        "chromashift=cbh=1:crh=-1",
    )

    # And one parameter off its identity is still the whole effect, both stages of it.
    assert stages([effect("lift_gamma_gain", gamma=1.2)]).treatment == (
        "colorbalance=rs=0:gs=0:bs=0:rh=0:gh=0:bh=0",
        "eq=gamma=1.2",
    )


def test_every_declared_parameter_reaches_the_composer_whether_it_was_sent_or_not():
    """The property that lets a composer index `values[...]` without a fallback: validation
    fills the stack in completely, so a composer never carries a second copy of a default."""
    resolved = validate_stack([{"effect": "lift_gamma_gain", "parameters": {"gamma": 1.5}}])
    assert dict(resolved[0].values) == {"lift": 0.0, "gamma": 1.5, "gain": 0.0}
    for definition in EFFECT_CATALOGUE.values():
        if any(isinstance(p, LutParameter) for p in definition.parameters):
            continue
        (only,) = validate_stack([{"effect": definition.effect_id}])
        assert set(only.values) == {parameter.name for parameter in definition.parameters}


def test_every_declared_default_is_pinned_as_a_literal():
    """The whole catalogue's defaults, written out, because the test above cannot do this.

    That one asserts `set(only.values) == {p.name for p in definition.parameters}` — an
    expectation read out of the same table the code reads. It proves **completeness**: no
    declared parameter goes missing on the way to a composer. It cannot prove **correctness**:
    a default that drifted from 1 to 0 satisfies it exactly as well, and a Contrast card that
    silently defaulted to 0 would ship a black frame with every test in this file green.

    So the twenty rows below are the contract. They are asserted on the *resolved values*
    rather than on stage text because an effect sitting at its identity now composes no stage
    at all — the point is that a default cannot drift unnoticed, not that it produces a filter.
    `lut_look` is the one entry with a parameter that declares no default at all, so its look
    is named here and its card switched off, which is what lets the folder go unconsulted.
    """
    expected: dict[str, dict[str, object]] = {
        "punch_in": {"zoom": 1.0},
        "handheld_shake": {"amplitude": 0.0, "frequency": 2.0},
        "dutch_tilt": {"angle": 0.0},
        "mirror": {"axis": "horizontal"},
        "grain": {"strength": 0.0, "seed": 0},
        "vignette": {"angle": 0.0},
        "soft_focus": {"sigma": 0.0},
        "sharpen": {"amount": 0.0},
        "banding_suppression": {"threshold": 0.0001},
        "lut_look": {"lut": "a-look-the-folder-need-not-hold", "interp": "tetrahedral"},
        "exposure": {"amount": 0.0},
        "contrast": {"amount": 1.0},
        "saturation": {"amount": 1.0},
        "temperature": {"amount": 0.0},
        "tint": {"amount": 0.0},
        "lift_gamma_gain": {"lift": 0.0, "gamma": 1.0, "gain": 0.0},
        "monochrome": {"amount": 1.0},
        "chroma_split": {"shift": 0.0},
        "posterize": {"levels": 256},
        "pixelate": {"size": 1},
    }
    assert set(expected) == set(EFFECT_CATALOGUE), "every catalogue entry must be pinned"

    for effect_id, values in expected.items():
        spec: dict[str, object] = {"effect": effect_id}
        if effect_id == "lut_look":
            spec["parameters"] = {"lut": values["lut"]}
            spec["enabled"] = False
        (resolved,) = validate_stack([spec])
        assert dict(resolved.values) == values, effect_id

    # Three of those are counts rather than fractions, and the difference is not visible in a
    # comparison — `0 == 0.0` — but it is visible in a filter string, where `seed=0.0` is not a
    # seed ffmpeg accepts. So the whole-number parameters are asserted to come back whole.
    for effect_id, parameter_name in (
        ("grain", "seed"),
        ("posterize", "levels"),
        ("pixelate", "size"),
    ):
        (resolved,) = validate_stack([{"effect": effect_id}])
        assert isinstance(resolved.values[parameter_name], int), effect_id


def test_an_unknown_effect_is_refused_by_name_and_nothing_is_composed():
    with pytest.raises(EffectRefusal) as refusal:
        stages([effect("grain", strength=4), effect("kaleidoscope")])
    assert str(refusal.value) == (
        "There is no effect called 'kaleidoscope' in the catalogue. Nothing was composed."
    )


def test_an_undeclared_parameter_is_refused_naming_the_effect_and_the_parameter():
    with pytest.raises(EffectRefusal) as refusal:
        stages([effect("vignette", angle=0.5, opacity=0.3)])
    message = str(refusal.value)
    assert "vignette has no parameter called 'opacity'" in message
    assert "It takes angle" in message


def test_a_value_past_a_bound_is_refused_naming_the_bound_it_broke():
    """Both bounds, and the sentence says which one — a refusal that only said "out of range"
    would leave a Director guessing which end they were at."""
    with pytest.raises(EffectRefusal) as low:
        stages([effect("punch_in", zoom=0.5)])
    assert str(low.value) == (
        "punch_in's zoom is 0.5, below its minimum of 1. Nothing was composed."
    )
    with pytest.raises(EffectRefusal) as high:
        stages([effect("grain", strength=61)])
    assert str(high.value) == (
        "grain's strength is 61, above its maximum of 60. Nothing was composed."
    )
    # The bounds themselves are inside. `punch_in` at its minimum is agreed and composes to
    # nothing, because a zoom of 1 is the identity — so the agreement is asserted on the
    # resolved values rather than on a stage that no longer exists.
    assert stages([effect("grain", strength=60)]).treatment[0].startswith("noise=alls=60")
    assert dict(validate_stack([effect("punch_in", zoom=1)])[0].values) == {"zoom": 1.0}


def test_a_value_of_the_wrong_type_is_refused_naming_the_offender():
    """A string where a number belongs, a flag where a number belongs (`bool` is an `int` in
    Python and would otherwise pass as 1), a fraction where a count belongs, and a NaN — which
    is a float, is inside every comparison, and would reach a filter string as `nan`."""
    for parameters, fragment in (
        ({"strength": "loud"}, "grain's strength must be a number, and 'loud' is not"),
        ({"strength": True}, "grain's strength must be a number, and True is not"),
        ({"seed": 1.5}, "grain's seed must be a whole number, and 1.5 is not"),
        ({"strength": float("nan")}, "grain's strength must be a finite number"),
    ):
        with pytest.raises(EffectRefusal) as refusal:
            stages([{"effect": "grain", "parameters": parameters}])
        assert fragment in str(refusal.value)


def test_an_integer_too_wide_for_a_double_is_refused_and_not_a_crash():
    """The one unusable number that used to leave by a different door.

    `float()` answers an `int` wider than a double with `OverflowError`, and a validation that
    converted before it checked raised it straight through `validate_stack` — past every
    `except EffectRefusal` a caller has. Measured on the write route and again at export: 500,
    500, zero jobs written. JSON puts no width on an integer literal, so a 401-digit `zoom` is
    something a client can genuinely send.

    `1e400` was never the same fault: it parses to `inf` and has always refused cleanly. Both
    forms are asserted here together so the two doors cannot drift apart again.
    """
    too_wide = int("9" * 401)
    sentence = (
        "punch_in's zoom is a whole number too large for this application to read as a number "
        "at all. It takes a number between 1 and 2. Nothing was composed."
    )
    for value in (too_wide, -too_wide):
        with pytest.raises(EffectRefusal) as refusal:
            stages([effect("punch_in", zoom=value)])
        assert str(refusal.value) == sentence
        # The refusal is the *only* thing that leaves. An `OverflowError` is a `ValueError`'s
        # sibling, not a subclass, so a bare `pytest.raises(ValueError)` above would have passed
        # against the bug — this is the assertion that would not have.
        with pytest.raises(EffectRefusal):
            validate_stack([effect("punch_in", zoom=value)])

    # The neighbouring wordings this fix deliberately did not reuse, held in place: `inf` is not
    # finite and says so, and an ordinary oversized value still reads as a bound broken.
    with pytest.raises(EffectRefusal) as infinite:
        stages([effect("punch_in", zoom=float("1e400"))])
    assert str(infinite.value) == (
        "punch_in's zoom must be a finite number, and inf is not. Nothing was composed."
    )
    with pytest.raises(EffectRefusal) as ordinary:
        stages([effect("punch_in", zoom=99)])
    assert str(ordinary.value) == (
        "punch_in's zoom is 99, above its maximum of 2. Nothing was composed."
    )


def test_a_choice_outside_its_set_is_refused_with_the_set_named():
    with pytest.raises(EffectRefusal) as refusal:
        stages([effect("mirror", axis="diagonal")])
    assert str(refusal.value) == (
        "mirror's axis must be one of horizontal, vertical, both, and 'diagonal' is not. "
        "Nothing was composed."
    )


def test_a_stack_entry_that_is_not_a_spec_is_refused_by_position():
    with pytest.raises(EffectRefusal) as not_a_map:
        stages([effect("grain"), "vignette"])  # type: ignore[list-item]
    assert "entry 1 of this stack is 'vignette'" in str(not_a_map.value)

    with pytest.raises(EffectRefusal) as unnamed:
        stages([{"parameters": {"strength": 3}}])
    assert "Entry 0 of this stack names no effect" in str(unnamed.value)

    with pytest.raises(EffectRefusal) as bad_parameters:
        stages([{"effect": "grain", "parameters": [3]}])
    assert "grain's parameters must be given by name" in str(bad_parameters.value)

    with pytest.raises(EffectRefusal) as bad_flag:
        stages([{"effect": "grain", "enabled": "yes"}])
    assert "grain is either enabled or it is not" in str(bad_flag.value)


def test_a_misspelled_top_level_key_is_refused_rather_than_ignored():
    """The level a client actually gets wrong, and the one the module docstring names: *"an
    ignored key is how a typo becomes an effect that quietly does nothing"*.

    `paramters` used to be dropped on the floor and the effect composed at its defaults — a
    grain card the Director set to 40 rendering as no grain at all, with nothing said. An
    undeclared parameter was already refused; this is the same refusal one level up, in the same
    sentence.
    """
    with pytest.raises(EffectRefusal) as typo:
        stages([{"effect": "grain", "paramters": {"strength": 40}}])
    assert str(typo.value) == (
        "grain has no key called 'paramters'. It takes effect, enabled, parameters. "
        "Nothing was composed."
    )
    with pytest.raises(EffectRefusal) as flag:
        stages([{"effect": "grain", "enabledd": False}])
    assert "grain has no key called 'enabledd'" in str(flag.value)
    # The three that are declared are, of course, all accepted together.
    assert stages([{"effect": "grain", "enabled": True, "parameters": {"strength": 4}}])


def test_a_refusal_prints_the_number_it_was_given_and_not_a_filter_rounding():
    """The sentence and the comparison that produced it must agree.

    The bound refusal used to format both the value and the bound through the *filter* formatter,
    whose six decimals exist so two float states compare equal in a chain. In a sentence that is
    a lie: any violation under half a millionth read `zoom is 1, below its minimum of 1`, and a
    value of 1e308 printed as a 309-digit integer because `.6f` never goes scientific.
    """
    with pytest.raises(EffectRefusal) as tiny:
        stages([effect("punch_in", zoom=1e-9)])
    assert str(tiny.value) == (
        "punch_in's zoom is 1e-09, below its minimum of 1. Nothing was composed."
    )
    with pytest.raises(EffectRefusal) as huge:
        stages([effect("punch_in", zoom=1e308)])
    assert str(huge.value) == (
        "punch_in's zoom is 1e+308, above its maximum of 2. Nothing was composed."
    )
    with pytest.raises(EffectRefusal) as near:
        stages([effect("punch_in", zoom=0.9999999)])
    assert str(near.value) == (
        "punch_in's zoom is 0.9999999, below its minimum of 1. Nothing was composed."
    )
    # And the *filter* formatter is untouched, six decimals and all: the two frequencies below
    # are rounded in the chain, which is the lossiness the sentence above must not inherit.
    assert stages(
        [effect("handheld_shake", amplitude=0.01, frequency=0.1234567)]
    ).geometry == (
        (
            "crop=w=iw*0.98:h=ih*0.98"
            ":x=(iw-ow)/2+iw*0.01*sin(2*PI*0.123457*t)"
            ":y=(ih-oh)/2+ih*0.01*cos(2*PI*0.169136*t)"
        ),
    )


def test_a_stack_that_is_not_a_list_at_all_is_refused_rather_than_raised_through():
    """`EffectRefusal` is the boundary, and the two shapes below used to leave as `TypeError`.

    A non-iterable stack, and a `parameters` map whose keys are not all of one type — `sorted`
    over `{1, 'opacity'}` raises rather than refusing. Both are low-reachability and both escape
    the only exception a caller has been told to catch.
    """
    for value in (None, 5, 2.5):
        with pytest.raises(EffectRefusal) as refusal:
            validate_stack(value)  # type: ignore[arg-type]
        assert str(refusal.value) == (
            f"An effect stack is a list of effects, and {value!r} is not. Nothing was composed."
        )
    with pytest.raises(EffectRefusal) as mixed:
        validate_stack([{"effect": "grain", "parameters": {1: 2, "opacity": 3}}])
    assert "grain has no parameter called 'opacity'" in str(mixed.value)
    with pytest.raises(EffectRefusal) as mixed_keys:
        validate_stack([{"effect": "grain", 1: 2, "colour": 3}])
    assert "grain has no key called" in str(mixed_keys.value)


def test_nothing_is_composed_when_anything_in_the_stack_is_refused():
    """"Nothing is composed" is not a figure of speech: the refusal happens before a single
    stage exists, so there is no half-built chain for a caller to use by mistake."""
    with pytest.raises(EffectRefusal):
        build_effect_stages(
            [effect("grain", strength=5), effect("contrast", amount=99)],
            width=EXPORT_WIDTH,
            height=EXPORT_HEIGHT,
        )


# ------------------------------------------------------------------------------------------
# The LUT folder.
# ------------------------------------------------------------------------------------------


def test_the_folder_is_a_sibling_of_projects_and_the_defaults_appear_on_first_run(
    tmp_path: Path,
):
    """First run: the folder does not exist, the generated set is written, and it is what is
    discovered. Beside `projects/`, never inside one — looks belong to the machine."""
    assert lut_directory(tmp_path) == tmp_path / "luts"
    assert not (tmp_path / "luts").exists()

    discovered = discover_luts(tmp_path)
    assert [entry.lut_id for entry in discovered] == sorted(
        lut_id for lut_id, _name, _transform in DEFAULT_LUTS
    )
    for entry in discovered:
        assert entry.path.parent == tmp_path / "luts"
        assert entry.path.is_file()


def test_the_defaults_are_written_once_and_never_argued_with(tmp_path: Path):
    """A Director who edits or deletes a generated look has made a decision. Generation is
    triggered by the folder's absence, and an individual file is never overwritten."""
    directory = lut_directory(tmp_path)
    write_default_luts(directory, size=5)
    edited = directory / "warm-shift.cube"
    edited.write_text("LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n" + "0 0 0\n" * 8)
    written = write_default_luts(directory, size=5)
    assert written == ()
    assert edited.read_text().startswith("LUT_3D_SIZE 2")

    # And a folder the Director emptied stays empty: it exists, so nothing regenerates.
    for path in directory.iterdir():
        path.unlink()
    assert discover_luts(tmp_path) == ()


def test_an_interrupted_first_run_leaves_no_half_written_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """"Never overwriting" and "written in place" are a trap together.

    Each of these is about a megabyte at the shipped lattice. Interrupted part-way through one —
    a closed lid, a killed process, a full disk — the old code left a truncated file that still
    carried its header, so it was still offered, and still existed, so the never-overwrite rule
    meant it was never regenerated. One interruption, and a look that fails at export forever.

    So the write goes to a temporary name and is moved onto the destination. Below, the third
    look's write is interrupted **half way through the bytes** — which is the only interruption
    that matters, and the reason this test patches the write rather than the generator: a run
    that dies before writing anything was never the problem. Nothing of that look survives, the
    two before it are whole, and the next run completes the set.
    """
    directory = lut_directory(tmp_path)
    real_write_text = Path.write_text

    def half_a_write(self: Path, data: str, *args: object, **kwargs: object) -> int:
        if "bleach-bypass" in self.name:
            real_write_text(self, data[: len(data) // 2], *args, **kwargs)  # type: ignore[arg-type]
            raise KeyboardInterrupt("the lid closed")
        return real_write_text(self, data, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", half_a_write)
    with pytest.raises(KeyboardInterrupt):
        write_default_luts(directory, size=8)
    monkeypatch.undo()

    # Written out by hand: the third of the five is the one that was interrupted, and nothing of
    # it is on disk — not a truncated `.cube`, and not a leftover temporary either.
    assert sorted(path.name for path in directory.iterdir()) == [
        "filmic-contrast.cube",
        "teal-and-orange.cube",
    ]

    assert [path.name for path in write_default_luts(directory, size=8)] == [
        "bleach-bypass.cube",
        "warm-shift.cube",
        "panchromatic-mono.cube",
    ]
    assert [entry.lut_id for entry in discover_luts(tmp_path)] == [
        "bleach-bypass",
        "filmic-contrast",
        "panchromatic-mono",
        "teal-and-orange",
        "warm-shift",
    ]


def test_a_directors_own_luts_are_indistinguishable_from_the_generated_ones(tmp_path: Path):
    """The whole point of discovering rather than bundling. A file dropped in is offered under
    an id derived from its name, exactly like the five this application generated."""
    directory = lut_directory(tmp_path)
    write_default_luts(directory, size=5)
    (directory / "Kodak 2383 D65.cube").write_text(
        cube_text(2, identity_transform), encoding="utf-8"
    )
    discovered = {entry.lut_id: entry for entry in discover_luts(tmp_path)}
    assert "kodak-2383-d65" in discovered
    assert discovered["kodak-2383-d65"].name == "Kodak 2383 D65"
    assert "filmic-contrast" in discovered


def test_anything_that_is_not_a_lut_is_ignored_rather_than_offered(tmp_path: Path):
    """A folder is a place people put things: notes, a half-copied download, a `.3dl` this
    application cannot read. None of it is offered, and none of it is a crash."""
    directory = lut_directory(tmp_path)
    directory.mkdir(parents=True)
    (directory / "notes.txt").write_text("remember to grade the chorus warmer")
    (directory / "look.3dl").write_text("3DMESH")
    (directory / "truncated.cube").write_text("TITLE \"half a download\"\n")
    (directory / "real.cube").write_text(cube_text(2, identity_transform), encoding="utf-8")
    (directory / "subfolder").mkdir()
    assert [entry.lut_id for entry in discover_luts(tmp_path)] == ["real"]


def test_a_half_copied_download_is_not_offered_as_a_look(tmp_path: Path):
    """The case the sniff was written for, and the case it used to pass.

    A half-copied download has `LUT_3D_SIZE N` on line 1 — the *end* is what is missing — so a
    header test accepts it, and ffmpeg then fails the export with `Error initializing filters`,
    which is neither the file's name nor a sentence anybody can act on. So the table is counted
    against the size the header declares.

    The count is deliberately a few lines slack — the header lines are counted with the data, so
    it asks for `N**3` lines of any kind — which is why the truncations below are gross rather
    than off-by-one. The last two files are the reason for that slack: a complete table is still
    offered when it carries a title, a comment and a blank line, because a sniff that drops a
    Director's real look is a worse failure than the one it prevents — a look that is not offered
    is invisible, where a refusal at export names itself.
    """
    directory = lut_directory(tmp_path)
    directory.mkdir(parents=True)
    whole = cube_text(8, identity_transform, title="Half A Download")
    (directory / "halfcopy.cube").write_text(whole[: len(whole) // 2], encoding="utf-8")
    (directory / "header-only.cube").write_text("LUT_3D_SIZE 8\n", encoding="utf-8")
    (directory / "short-by-ten.cube").write_text(
        "\n".join(whole.splitlines()[:-10]) + "\n", encoding="utf-8"
    )
    (directory / "whole.cube").write_text(whole, encoding="utf-8")
    (directory / "chatty.cube").write_text(
        'TITLE "Chatty"\n# graded on the 21st\n\n' + cube_text(4, identity_transform),
        encoding="utf-8",
    )
    assert [entry.lut_id for entry in discover_luts(tmp_path)] == ["chatty", "whole"]


def test_two_files_whose_names_collide_get_stable_distinct_ids(tmp_path: Path):
    """The id is lossy by design, so a collision is possible. Every member of a collision set is
    suffixed with a digest of its **own** filename — nobody keeps the bare base, and nothing an
    id points at depends on what else is in the folder.

    The ids below are written out by hand. They are the contract a manifest stores, so a change
    to how they are derived has to be a change to this list, not a value this test recomputes
    from the same function it is checking.
    """
    directory = lut_directory(tmp_path)
    directory.mkdir(parents=True)
    for name in ("Warm Shift.cube", "warm_shift.cube", "warm-shift.cube"):
        (directory / name).write_text(cube_text(2, identity_transform), encoding="utf-8")
    ids = [entry.lut_id for entry in discover_luts(tmp_path)]
    assert ids == ["warm-shift-a88a519f", "warm-shift-8fc81c18", "warm-shift-aa624071"]
    assert ids == [entry.lut_id for entry in discover_luts(tmp_path)]
    assert "warm-shift" not in ids
    # A file that is the only holder of its base keeps the bare id — the ordinary case, and the
    # whole of the Director's own 48-file pack.
    (directory / "Kodak 2383.cube").write_text(
        cube_text(2, identity_transform), encoding="utf-8"
    )
    assert "kodak-2383" in [entry.lut_id for entry in discover_luts(tmp_path)]
    assert lut_id_for_name("Kodak 2383 (D65)!") == "kodak-2383-d65"
    assert lut_id_for_name("...") == "lut"


def test_deleting_one_look_never_silently_retargets_another(tmp_path: Path):
    """A LUT id is stored in a manifest, so it has to be a handle on a *file*.

    It used to be a handle on a position: the collision suffix counted up the sorted listing, so
    `my-look` was whichever colliding file happened to sort first. Delete that one, and the id
    a manifest was holding went on grading — through a different file, with no refusal and
    nothing visible anywhere. This is that sequence, and the assertion is that the stale id is
    now *refused* rather than quietly answered by the survivor.
    """
    directory = lut_directory(tmp_path)
    directory.mkdir(parents=True)
    for name in ("My Look.cube", "my-look.cube"):
        (directory / name).write_text(cube_text(2, identity_transform), encoding="utf-8")
    before = {entry.lut_id: entry.path.name for entry in discover_luts(tmp_path)}
    assert before == {"my-look-c9021654": "My Look.cube", "my-look-21c1a34c": "my-look.cube"}

    (directory / "My Look.cube").unlink()
    after = discover_luts(tmp_path)
    assert {entry.lut_id: entry.path.name for entry in after} == {"my-look": "my-look.cube"}
    with pytest.raises(EffectRefusal) as refusal:
        stages([effect("lut_look", lut="my-look-c9021654")], luts=after)
    assert str(refusal.value) == (
        "There is no look called 'my-look-c9021654' in the looks folder. Nothing was composed."
    )


def test_a_grade_names_a_lut_by_id_and_the_path_comes_from_the_server(tmp_path: Path):
    """The security property, stated as a test: the client sends an id, and the only path that
    reaches the filter is the one discovery produced."""
    write_default_luts(lut_directory(tmp_path), size=5)
    luts = discover_luts(tmp_path)
    warm = next(entry for entry in luts if entry.lut_id == "warm-shift")
    built = stages([effect("lut_look", lut="warm-shift")], luts=luts)
    assert built.treatment == (
        f"lut3d=file={lut_file_argument(warm.path)}:interp=tetrahedral",
    )
    assert warm.path.as_posix().replace(":", r"\:") in built.treatment[0]


def test_a_lut_id_the_folder_does_not_hold_is_refused_by_name(tmp_path: Path):
    write_default_luts(lut_directory(tmp_path), size=5)
    luts = discover_luts(tmp_path)
    with pytest.raises(EffectRefusal) as refusal:
        stages([effect("lut_look", lut="../../../etc/passwd")], luts=luts)
    assert str(refusal.value) == EFFECT_LUT_UNKNOWN_REFUSAL.format(lut="../../../etc/passwd")

    with pytest.raises(EffectRefusal) as unnamed:
        stages([effect("lut_look")], luts=luts)
    assert "lut_look needs a look chosen" in str(unnamed.value)


def test_a_lut_discovered_and_then_deleted_is_reported_by_id(tmp_path: Path):
    """Not silently skipped, and not a crash. The look was real when the panel listed it and is
    gone by the time the export runs, which is a sentence a Director can act on."""
    write_default_luts(lut_directory(tmp_path), size=5)
    luts = discover_luts(tmp_path)
    warm = next(entry for entry in luts if entry.lut_id == "warm-shift")
    warm.path.unlink()
    with pytest.raises(EffectRefusal) as refusal:
        stages([effect("lut_look", lut="warm-shift")], luts=luts)
    assert str(refusal.value) == EFFECT_LUT_FILE_MISSING_REFUSAL.format(
        lut="warm-shift", path=warm.path.as_posix()
    )


def test_the_drive_letter_colon_never_reaches_ffmpegs_option_parser():
    r"""Measured 2026-08-25 against this project's ffmpeg 7.0 — see the module docstring's table.

    Single-quoted with the colon escaped. The unquoted escape and the cwd-relative form both
    break on a path containing a comma or a semicolon; this one survives everything but an
    apostrophe, which is refused by name below rather than left to fail inside ffmpeg with a
    message that names `clut` and mentions neither the path nor the problem.
    """
    assert lut_file_argument(Path(r"F:\Music Video\data\luts\warm-shift.cube")) == (
        "'F\\:/Music Video/data/luts/warm-shift.cube'"
    )
    assert lut_file_argument(Path("/srv/data/luts/warm-shift.cube")) == (
        "'/srv/data/luts/warm-shift.cube'"
    )
    assert lut_file_argument(Path(r"F:\a,b;c\warm.cube")) == "'F\\:/a,b;c/warm.cube'"
    with pytest.raises(EffectRefusal) as refusal:
        lut_file_argument(Path("F:/Director's looks/warm.cube"), lut_id="warm")
    assert "contains an apostrophe" in str(refusal.value)


# ------------------------------------------------------------------------------------------
# The generated `.cube` format.
# ------------------------------------------------------------------------------------------


def test_the_cube_header_is_written_the_only_way_ffmpeg_reads_it():
    """`LUT_3D_SIZE N` is the only mandatory line and everything before it is ignored, which is
    why `TITLE` may lead. The identity domain is always written, because ffmpeg computes
    `scale = clip(1/(max-min), 0, 1)` and never subtracts `min` — a `DOMAIN_MIN` offset is
    silently ignored and a `DOMAIN_MAX` under 1 is silently clamped away."""
    text = cube_text(2, identity_transform, title="Identity")
    lines = text.splitlines()
    assert lines[:4] == [
        'TITLE "Identity"',
        "LUT_3D_SIZE 2",
        "DOMAIN_MIN 0 0 0",
        "DOMAIN_MAX 1 1 1",
    ]
    assert len(lines) == 4 + 2**3
    assert text.endswith("\n")
    with pytest.raises(ValueError):
        cube_text(1, identity_transform)


def test_red_varies_fastest_then_green_then_blue():
    """The nesting, asserted on the table itself. Written as nested loops the *outer* one is
    blue; get it backwards and nothing anywhere reports an error — the picture simply comes back
    with red and blue exchanged. The PSNR test below is the same fact measured through ffmpeg."""
    lines = cube_text(2, identity_transform).splitlines()[3:]
    assert lines[0] == "0.000000 0.000000 0.000000"
    assert lines[1] == "1.000000 0.000000 0.000000"  # red moved first
    assert lines[2] == "0.000000 1.000000 0.000000"  # then green
    assert lines[4] == "0.000000 0.000000 1.000000"  # and blue last


def test_the_generated_looks_stay_inside_the_domain_and_are_pinned_to_their_own_arithmetic():
    """Clamped, so a look cannot write a value ffmpeg would clip into a highlight nobody asked
    for — and pinned, so "reproducible" means something.

    The reproducibility half used to be `text == cube_text(5, transform, title=title)`: the
    same pure function called twice with the same arguments, which is true of any function at
    all and would go on being true if every look in the set were replaced by a different one.
    What a generated render input actually owes is that **the same bytes come out today as came
    out when the look shipped**, and the only way to say that is to write the bytes down.

    Two of the five are written out whole at a lattice of 2, which is the smallest size that
    still visits both ends of every axis and is therefore where the arithmetic is legible:

    * `panchromatic-mono` is `0.30r + 0.59g + 0.11b` — the *film* weights, not the Rec.709 ones
      the rest of this module uses, which is the whole reason the look exists. Every corner of
      the cube lands on a distinct grey, so a slipped weight moves a number here.
    * `warm-shift` is a gain of 1.08, 1.01 and 0.90 — and at a lattice of 2 the red and green
      corners both exceed 1 and come back clamped, which is the clamp asserted as a value
      rather than as a range check that a missing clamp could still pass on some other look.
    """
    looks = {lut_id: transform for lut_id, _title, transform in DEFAULT_LUTS}

    assert cube_text(2, looks["panchromatic-mono"], title="Panchromatic Mono") == (
        'TITLE "Panchromatic Mono"\n'
        "LUT_3D_SIZE 2\n"
        "DOMAIN_MIN 0 0 0\n"
        "DOMAIN_MAX 1 1 1\n"
        "0.000000 0.000000 0.000000\n"
        "0.300000 0.300000 0.300000\n"
        "0.590000 0.590000 0.590000\n"
        "0.890000 0.890000 0.890000\n"
        "0.110000 0.110000 0.110000\n"
        "0.410000 0.410000 0.410000\n"
        "0.700000 0.700000 0.700000\n"
        "1.000000 1.000000 1.000000\n"
    )
    assert cube_text(2, looks["warm-shift"], title="Warm Shift") == (
        'TITLE "Warm Shift"\n'
        "LUT_3D_SIZE 2\n"
        "DOMAIN_MIN 0 0 0\n"
        "DOMAIN_MAX 1 1 1\n"
        "0.000000 0.000000 0.000000\n"
        "1.000000 0.000000 0.000000\n"
        "0.000000 1.000000 0.000000\n"
        "1.000000 1.000000 0.000000\n"
        "0.000000 0.000000 0.900000\n"
        "1.000000 0.000000 0.900000\n"
        "0.000000 1.000000 0.900000\n"
        "1.000000 1.000000 0.900000\n"
    )

    # And every look in the set stays inside the domain at a lattice fine enough to reach the
    # midtones, where the two S-curves actually bend.
    for lut_id, title, transform in DEFAULT_LUTS:
        for line in cube_text(5, transform, title=title).splitlines()[4:]:
            assert all(0.0 <= float(value) <= 1.0 for value in line.split()), lut_id


def test_the_default_lattice_is_33(tmp_path: Path):
    """Measured during research: 330 ms per 120 1080p frames at 33 against 319 ms at 17, and 17
    visibly quantises gradients. So the grid is very nearly free and this is what ships."""
    assert DEFAULT_LUT_SIZE == 33
    written = write_default_luts(lut_directory(tmp_path))
    assert len(written) == len(DEFAULT_LUTS)
    first = written[0].read_text(encoding="utf-8").splitlines()
    assert "LUT_3D_SIZE 33" in first
    assert len(first) == 4 + 33**3


# ------------------------------------------------------------------------------------------
# The three tests that run the real binary.
# ------------------------------------------------------------------------------------------


def ffmpeg(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True, check=False
    )


def frame_grid(rendered: Path) -> tuple[int, int, int]:
    """`(width, height, frames)` of a rendered file, counted rather than read off a header.

    `-count_frames` decodes the stream instead of trusting `nb_frames`, which a container may
    not carry at all and which a filter that dropped frames would not correct. The design note
    behind this slice stakes everything on *"the assembled video matches the song within one
    frame, for every combination of effects"*, and until this existed the three real renders in
    this file asserted only that ffmpeg exited zero.
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,nb_read_frames",
            "-of", "default=noprint_wrappers=1",
            rendered.as_posix(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.strip()
    # Read by key rather than by position: ffprobe prints these in its own fixed order, not in
    # the order `-show_entries` asked for.
    fields = dict(line.split("=", 1) for line in result.stdout.split() if "=" in line)
    return (int(fields["width"]), int(fields["height"]), int(fields["nb_read_frames"]))


def test_a_generated_identity_round_trips_through_lut3d_and_a_wrong_nesting_does_not(
    tmp_path: Path,
):
    """The one mistake a `.cube` writer makes silently, measured rather than reasoned about.

    An identity written red-fastest comes back through `lut3d` at ~84 dB PSNR — the residue of
    eight-bit quantisation and nothing else. The same table written blue-fastest scores under
    5 dB with red and blue exchanged and green untouched, and **ffmpeg reports no error at all**
    for it. Nothing except rendering the file can tell the two apart.
    """
    source = tmp_path / "source.mp4"
    assert (
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1:r=5", "-frames:v", "5", str(source)
        ).returncode
        == 0
    )

    right = tmp_path / "right.cube"
    right.write_text(cube_text(33, identity_transform), encoding="utf-8")

    # The same table, deliberately written with the loops nested the other way round.
    size, last = 33, 32
    wrong_lines = ["LUT_3D_SIZE 33", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    for red_step in range(size):
        for green_step in range(size):
            for blue_step in range(size):
                wrong_lines.append(
                    f"{red_step / last:.6f} {green_step / last:.6f} {blue_step / last:.6f}"
                )
    wrong = tmp_path / "wrong.cube"
    wrong.write_text("\n".join(wrong_lines) + "\n", encoding="utf-8")

    def psnr(cube: Path) -> float:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "info", "-i", str(source), "-i", str(source),
                "-filter_complex",
                f"[0:v]lut3d=file={lut_file_argument(cube)}[graded];[graded][1:v]psnr",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        line = next(line for line in result.stderr.splitlines() if "PSNR" in line)
        return float(line.split("average:")[1].split()[0])

    assert psnr(right) > 60.0
    assert psnr(wrong) < 10.0

    # And the same identity, loaded from a directory whose name holds every character class the
    # quoting rule claims to survive. Until this line, the *only* paths that reached real ffmpeg
    # in this suite were under `tmp_path`, which contains no space, comma, semicolon, bracket,
    # percent, ampersand or equals sign — so `test_the_drive_letter_colon_never_reaches_ffmpegs
    # _option_parser` asserted four strings and the binary evidence covered exactly one
    # character class, the drive-letter colon. The comma and the semicolon are the two that
    # break every other escaping form in the module docstring's table, and the percent sign is
    # the one nothing in this file would notice if the escaper started rewriting it.
    awkward = tmp_path / "a b,c;d[e]%f&g=h"
    awkward.mkdir()
    hostile = awkward / "identity.cube"
    hostile.write_text(cube_text(33, identity_transform), encoding="utf-8")
    assert psnr(hostile) > 60.0


def test_a_texture_before_pad_leaves_the_letterbox_bars_pure_black(tmp_path: Path):
    """The manual check, automated — and the one claim in this slice a string comparison cannot
    make. Measured 2026-08-21: a 4:3 source into a 16:9 target samples RGB `(1,1,5)` in the bar
    with the texture after `pad`, and `(0,0,0)` with it before.

    A real trim is run through the argv this application builds, and the pillarbox is sampled
    out of the decoded frame. The after-`pad` chain is built alongside it and asserted *dirty*,
    so the test proves the ordering is what makes the difference rather than proving that black
    bars happen to be black.
    """
    source = tmp_path / "four-by-three.mp4"
    assert (
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=s=640x480:d=1:r=24", "-frames:v", "24",
            "-pix_fmt", "yuv420p", str(source),
        ).returncode
        == 0
    )

    built = stages([effect("grain", strength=40, seed=7), effect("vignette", angle=1.0)])
    correct = tmp_path / "before-pad.mp4"
    assert (
        subprocess.run(
            trim_args(
                source,
                correct,
                frames=12,
                width=1056,
                height=608,
                treatment_stages=built.treatment,
            ),
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
    )

    def bars(rendered: Path) -> np.ndarray:
        raw = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-i", str(rendered), "-frames:v", "6",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ],
            capture_output=True,
            check=False,
        ).stdout
        frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 608, 1056, 3)
        # 640x480 into 1056x608 fits to 810x608, so the pillars are ~123 px on each side.
        return np.concatenate([frames[:, :, :100, :], frames[:, :, -100:, :]], axis=2)

    assert int(bars(correct).max()) == 0

    # The same treatments after `pad` instead: the bar is no longer black. This is the half of
    # the assertion that makes the half above mean something.
    dirty = tmp_path / "after-pad.mp4"
    misordered = trim_args(source, dirty, frames=12, width=1056, height=608)
    index = misordered.index("-vf") + 1
    chain = misordered[index].split(",")
    at_pad = chain.index("pad=1056:608:(ow-iw)/2:(oh-ih)/2")
    misordered[index] = ",".join(
        chain[: at_pad + 1] + list(built.treatment) + chain[at_pad + 1 :]
    )
    assert subprocess.run(misordered, capture_output=True, check=False).returncode == 0
    assert int(bars(dirty).max()) > 0


def test_pixelate_does_not_change_the_frames_size_and_pad_adds_no_border(tmp_path: Path):
    """A treatment may not resize the frame, and this one did.

    `scale=iw/N` truncates, so `scale=iw*N` cannot restore a size N does not divide. At the
    export this application actually uses, 1056x608, a block size of 64 handed `pad` a 1024x576
    frame and `pad` centred it inside a 16-pixel black border on all four sides — on a shot with
    no letterbox at all. The source below is **entirely white**, so any black pixel in the output
    is a border and nothing else, and the corner sampled `00 00 00` before this was fixed.

    Two block sizes, neither of which divides the frame, and one that does: the acceptance sweep
    exercises size 4 on 320x240, where both divisions come out exact, which is why the bug
    survived it.
    """
    for width, height, size in ((1056, 608, 64), (1920, 1080, 7), (320, 240, 4)):
        source = tmp_path / f"white-{width}x{height}.mp4"
        assert (
            ffmpeg(
                "-f", "lavfi", "-i", f"color=c=white:s={width}x{height}:d=1:r=24",
                "-frames:v", "8", "-pix_fmt", "yuv420p", str(source),
            ).returncode
            == 0
        )
        built = build_effect_stages(
            [effect("pixelate", size=size)], width=width, height=height
        )
        assert built.treatment == (f"pixelize=w={size}:h={size}:mode=avg",)

        rendered = tmp_path / f"pixelated-{width}-{size}.mp4"
        assert (
            subprocess.run(
                trim_args(
                    source,
                    rendered,
                    frames=4,
                    width=width,
                    height=height,
                    treatment_stages=built.treatment,
                ),
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        raw = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-i", str(rendered), "-frames:v", "4",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ],
            capture_output=True,
            check=False,
        ).stdout
        frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, height, width, 3)
        assert frames.shape[0] == 4
        # A white source, pixelated: every pixel of every frame is still white, and in
        # particular no edge of it is the black `pad` used to put there.
        assert int(frames.min()) == 255, (width, size)


def test_every_stage_the_catalogue_can_emit_is_accepted_by_this_projects_ffmpeg(tmp_path: Path):
    """A typo in a filter option is a failed test here rather than a failed export later.

    Every effect is composed at a non-default value — a default is often a no-op that ffmpeg
    would accept even with a misspelled option elsewhere in the same filter — and run through
    the real chain, geometry group and treatment group in their real positions.
    """
    write_default_luts(lut_directory(tmp_path), size=5)
    luts = discover_luts(tmp_path)
    exercised: dict[str, dict[str, object]] = {
        "punch_in": {"zoom": 1.4},
        "handheld_shake": {"amplitude": 0.03, "frequency": 3.5},
        "dutch_tilt": {"angle": -8.5},
        "mirror": {"axis": "both"},
        "grain": {"strength": 18, "seed": 12345},
        "vignette": {"angle": 0.9},
        "soft_focus": {"sigma": 3.5},
        "sharpen": {"amount": 1.25},
        "banding_suppression": {"threshold": 0.02},
        "lut_look": {"lut": luts[0].lut_id, "interp": "trilinear"},
        "exposure": {"amount": 0.2},
        "contrast": {"amount": 1.6},
        "saturation": {"amount": 0.4},
        "temperature": {"amount": -0.35},
        "tint": {"amount": 0.3},
        "lift_gamma_gain": {"lift": 0.05, "gamma": 1.4, "gain": -0.1},
        "monochrome": {"amount": 0.75},
        "chroma_split": {"shift": 0.008},
        "posterize": {"levels": 6},
        "pixelate": {"size": 4},
    }
    assert set(exercised) == set(EFFECT_CATALOGUE), "every catalogue entry must be exercised"

    source = tmp_path / "source.mp4"
    assert (
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1:r=24", "-frames:v", "24",
            "-pix_fmt", "yuv420p", str(source),
        ).returncode
        == 0
    )
    for effect_id, parameters in exercised.items():
        built = build_effect_stages(
            [{"effect": effect_id, "parameters": parameters}],
            width=320,
            height=240,
            luts=luts,
        )
        dest = tmp_path / f"{effect_id}.mp4"
        result = subprocess.run(
            trim_args(
                source,
                dest,
                frames=6,
                width=320,
                height=240,
                geometry_stages=built.geometry,
                treatment_stages=built.treatment,
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{effect_id}: {result.stderr.strip()}"
        assert dest.is_file(), effect_id
        # `returncode == 0` is a syntax gate. The frame grid is the semantic one: six frames
        # were asked for and six must come back, at the export's own geometry, because an
        # effect that dropped or duplicated a frame would put every later shot out of sync
        # with the song and would exit zero doing it.
        assert frame_grid(dest) == (320, 240, 6), effect_id


def test_all_twenty_effects_stacked_at_once_render_through_the_real_chain(tmp_path: Path):
    """One render of a *combination*, because every other real render in this file is one
    effect — or grain and a vignette — and combinations are string-only otherwise.

    The whole catalogue at once is the extreme of the matrix: four families, both insertion
    points, two effects that compose to more than one stage, a geometry group that crops three
    separate times before `scale` and a treatment group of seventeen filters between `scale`
    and `pad`. It is also the only place the escaped comma inside Dutch Tilt's `max()` shares a
    chain with twenty-two other comma-separated stages.

    The frame grid is the assertion that matters: twenty filters deep, six frames in at the
    export's geometry must still be six frames out at the export's geometry.
    """
    write_default_luts(lut_directory(tmp_path), size=5)
    luts = discover_luts(tmp_path)
    everything = [
        effect("punch_in", zoom=1.2),
        effect("handheld_shake", amplitude=0.02, frequency=3),
        effect("dutch_tilt", angle=-6),
        effect("mirror", axis="both"),
        effect("grain", strength=14, seed=99),
        effect("vignette", angle=0.7),
        effect("soft_focus", sigma=1.5),
        effect("sharpen", amount=0.8),
        effect("banding_suppression", threshold=0.01),
        effect("lut_look", lut=luts[0].lut_id, interp="trilinear"),
        effect("exposure", amount=0.1),
        effect("contrast", amount=1.3),
        effect("saturation", amount=0.7),
        effect("temperature", amount=0.25),
        effect("tint", amount=-0.2),
        effect("lift_gamma_gain", lift=0.03, gamma=1.1, gain=-0.04),
        effect("monochrome", amount=0.4),
        effect("chroma_split", shift=0.006),
        effect("posterize", levels=12),
        effect("pixelate", size=3),
    ]
    assert {spec["effect"] for spec in everything} == set(EFFECT_CATALOGUE)

    built = build_effect_stages(everything, width=320, height=240, luts=luts)
    # Six geometry stages before `scale` from four effects — Dutch Tilt is two of them and
    # Mirror on both axes is two more — and seventeen treatments from sixteen effects, the
    # extra being Lift/Gamma/Gain's inseparable pair.
    assert len(built.geometry) == 6
    assert len(built.treatment) == 17

    source = tmp_path / "source.mp4"
    assert (
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1:r=24", "-frames:v", "24",
            "-pix_fmt", "yuv420p", str(source),
        ).returncode
        == 0
    )
    dest = tmp_path / "everything.mp4"
    result = subprocess.run(
        trim_args(
            source,
            dest,
            frames=6,
            width=320,
            height=240,
            geometry_stages=built.geometry,
            treatment_stages=built.treatment,
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.strip()
    assert frame_grid(dest) == (320, 240, 6)


def test_the_catalogue_covers_all_four_families_with_bounded_declared_parameters():
    """Structural, and cheap: every entry is in a known family, every number is bounded with its
    default inside its own bounds, and every choice's default is one of its choices. A catalogue
    entry that failed any of these would be a control the panel could not draw."""
    families = {definition.family for definition in EFFECT_CATALOGUE.values()}
    assert families == {FAMILY_GEOMETRY, FAMILY_TEXTURE, FAMILY_GRADE, FAMILY_STYLIZE}
    for effect_id, definition in EFFECT_CATALOGUE.items():
        assert definition.effect_id == effect_id
        assert definition.parameters, effect_id
        names = [parameter.name for parameter in definition.parameters]
        assert len(names) == len(set(names)), effect_id
        for parameter in definition.parameters:
            if isinstance(parameter, NumberParameter):
                assert parameter.minimum <= parameter.default <= parameter.maximum, effect_id
                assert parameter.minimum < parameter.maximum, effect_id
            elif isinstance(parameter, ChoiceParameter):
                assert parameter.default in parameter.choices, effect_id


def test_a_lut_entry_never_takes_its_path_from_the_stack(tmp_path: Path):
    """Belt and braces on the one place a client string could have become a path.

    The filename and the id are **deliberately different words**. `Grade 07 (Final).cube` is
    discovered under the id `grade-07-final`, because the id is lowercased, hyphenated and
    stripped of punctuation — so a stage built by interpolating the client's string would read
    `grade-07-final` where the real one reads `Grade 07 (Final).cube`, and the two are
    distinguishable at a glance. The previous version of this test used a fixture whose stem
    and id were the same word, checked a substring rather than the stage, and recomputed the
    colon escape with the same expression the code uses: it would have passed against a stub
    that simply interpolated whatever the stack sent.

    So: the whole stage is compared, the escape is built by splitting at the first colon rather
    than by rewriting every colon the way `lut_file_argument` does, and the client's own string
    is asserted **absent** from the result.
    """
    directory = lut_directory(tmp_path)
    directory.mkdir(parents=True)
    server_file = directory / "Grade 07 (Final).cube"
    server_file.write_text(cube_text(2, identity_transform), encoding="utf-8")
    luts = discover_luts(tmp_path)
    assert luts == (
        LutEntry(lut_id="grade-07-final", name="Grade 07 (Final)", path=server_file),
    )

    built = stages([effect("lut_look", lut="grade-07-final")], luts=luts)

    # `tmp_path` is the one part of the expectation that cannot be a literal, so it is taken
    # from the folder this test made — never from the entry the code returned. The drive
    # letter's colon is escaped by hand: split once at the first colon, put `\:` back.
    posix = directory.as_posix()
    head, colon, tail = posix.partition(":")
    escaped = f"{head}\\:{tail}" if colon else posix
    assert built.treatment == (
        f"lut3d=file='{escaped}/Grade 07 (Final).cube':interp=tetrahedral",
    )
    # And nothing the client sent survives into the filter string.
    assert "grade-07-final" not in built.treatment[0]


def test_the_same_stack_renders_the_same_frames_twice_and_the_grain_seed_is_load_bearing(
    tmp_path: Path,
):
    """FX-8's determinism clause, measured on the frames the chain produces.

    Grain is the one effect here that could break it. `noise` without `all_seed` is seeded from
    the clock, and an export would then differ on every run while every string comparison in
    this file went on passing — the chain text would be identical and the pictures would not.
    So the seed is always written, and the second half of this test proves it is reaching ffmpeg
    rather than being a number the composer prints and the filter ignores.

    **The comparison is of the filter graph's output, not of the encoded file, and that is a
    measured decision.** Encoding the same frames twice through this project's own
    `libx264 -preset veryfast` does *not* produce the same bitstream: measured 2026-08-25, eight
    runs of an identical grained chain produced two distinct pictures, and forcing the encoder to
    a single thread collapsed them to one. Multi-threaded libx264 is not bit-exact on
    high-entropy input. That is a property of the export encoder, not of this chain — a
    grain-free render is stable only because its input is not entropic enough to expose it — and
    fixing it would cost every export its encoder threads. So this test asserts what this slice
    actually owns: the same stack yields the same frames out of the filter graph.
    """
    source = tmp_path / "source.mp4"
    assert (
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1:r=24", "-frames:v", "24",
            "-pix_fmt", "yuv420p", str(source),
        ).returncode
        == 0
    )

    def frames(seed: int) -> bytes:
        built = build_effect_stages(
            [effect("grain", strength=30, seed=seed)], width=320, height=240
        )
        chain = trim_args(
            source,
            tmp_path / "unused.mp4",
            frames=12,
            width=320,
            height=240,
            treatment_stages=built.treatment,
        )
        return subprocess.run(
            [
                "ffmpeg", "-v", "error", "-i", str(source),
                "-vf", chain[chain.index("-vf") + 1],
                "-frames:v", "12", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-",
            ],
            capture_output=True,
            check=False,
        ).stdout

    grained = frames(9)
    assert len(grained) == 12 * 320 * 240 * 3 // 2
    assert grained == frames(9)
    assert grained != frames(10)
