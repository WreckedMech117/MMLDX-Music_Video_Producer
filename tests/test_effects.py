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

import dataclasses
import itertools
import subprocess
from pathlib import Path

import numpy as np
import pytest

from music_video_producer.assembly import trim_args
from music_video_producer.config import Settings
from music_video_producer.effects import (
    BINDING_NO_ENVELOPE_REFUSAL,
    BRANCH_FRAME_GUARD,
    BRANCH_LEG_FORMAT,
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
    LOOK_PROBE_HEIGHT,
    LOOK_PROBE_WIDTH,
    ONE_SIDED_FORMS,
    ONE_SIDED_TRANSITION_FRAMES,
    ONE_SIDED_TRANSITION_LABEL,
    OPENING_FORMS,
    OPENING_TRANSITION_LABEL,
    PRE_PAD_FAMILIES,
    PRE_SCALE_FAMILIES,
    PREVIEW_FINGERPRINT_INPUTS,
    TRANSITION_CATALOGUE,
    TRANSITION_UNKNOWN_REFUSAL,
    ChoiceParameter,
    EffectRefusal,
    EffectStages,
    LutEntry,
    LutParameter,
    NumberParameter,
    StageContext,
    build_effect_stages,
    cube_text,
    discover_luts,
    exported_bindings,
    exported_look,
    identity_transform,
    lut_directory,
    lut_file_argument,
    lut_id_for_name,
    one_sided_transition_stages,
    opening_transition_stages,
    preview_fingerprint,
    transition_definition,
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
            ":x=(iw-ow)/2+iw*0.03*sin(2*PI*3.5*(t+0))"
            ":y=(ih-oh)/2+ih*0.03*cos(2*PI*4.795*(t+0))"
        ),
    )
    # At the amplitude's own maximum the inset is at its largest: 1 - 2*0.05 = 0.9, a window
    # nine tenths of the frame, and 10 * 1.37 = 13.7 on the vertical.
    assert stages([effect("handheld_shake", amplitude=0.05, frequency=10)]).geometry == (
        (
            "crop=w=iw*0.9:h=ih*0.9"
            ":x=(iw-ow)/2+iw*0.05*sin(2*PI*10*(t+0))"
            ":y=(ih-oh)/2+ih*0.05*cos(2*PI*13.7*(t+0))"
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
            ":x=(iw-ow)/2+iw*0.02*sin(2*PI*2*(t+0))"
            ":y=(ih-oh)/2+ih*0.02*cos(2*PI*2.74*(t+0))"
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

    So the twenty-five rows below are the contract. They are asserted on the *resolved values*
    rather than on stage text because an effect sitting at its identity now composes no stage
    at all — the point is that a default cannot drift unnoticed, not that it produces a filter.
    `lut_look` is the one entry with a parameter that declares no default at all, so its look
    is named here and its card switched off, which is what lets the folder go unconsulted.
    """
    expected: dict[str, dict[str, object]] = {
        "punch_in": {"zoom": 1.0},
        "slow_zoom": {"zoom": 1.0, "direction": "in"},
        "handheld_shake": {"amplitude": 0.0, "frequency": 2.0},
        "dutch_tilt": {"angle": 0.0},
        "mirror": {"axis": "horizontal"},
        "grain": {"strength": 0.0, "seed": 0},
        "vignette": {"angle": 0.0},
        "soft_focus": {"sigma": 0.0},
        "sharpen": {"amount": 0.0},
        "banding_suppression": {"threshold": 0.0001},
        "bloom": {"intensity": 0.0, "threshold": 0.7, "radius": 8.0},
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
        "edge_treatment": {"strength": 0.0, "low": 0.08, "high": 0.2},
        "scanlines": {"strength": 0.0, "lines": 200},
        "pixel_shuffle": {"amount": 0.0, "block": 8, "seed": 0},
    }
    assert set(expected) == set(EFFECT_CATALOGUE), "every catalogue entry must be pinned"

    for effect_id, values in expected.items():
        spec: dict[str, object] = {"effect": effect_id}
        if effect_id == "lut_look":
            spec["parameters"] = {"lut": values["lut"]}
            spec["enabled"] = False
        (resolved,) = validate_stack([spec])
        assert dict(resolved.values) == values, effect_id

    # Six of those are counts rather than fractions, and the difference is not visible in a
    # comparison — `0 == 0.0` — but it is visible in a filter string, where `seed=0.0` is not a
    # seed ffmpeg accepts. So the whole-number parameters are asserted to come back whole.
    for effect_id, parameter_name in (
        ("grain", "seed"),
        ("posterize", "levels"),
        ("pixelate", "size"),
        ("scanlines", "lines"),
        ("pixel_shuffle", "block"),
        ("pixel_shuffle", "seed"),
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
        "grain has no key called 'paramters'. It takes id, effect, enabled, parameters, bindings. "
        "Nothing was composed."
    )
    with pytest.raises(EffectRefusal) as flag:
        stages([{"effect": "grain", "enabledd": False}])
    assert "grain has no key called 'enabledd'" in str(flag.value)
    # The five that are declared are, of course, all accepted together. `bindings` joined them
    # for Epic 10 and `id` for R-33; the id is read by nothing in this module, which is what this
    # row asserts by composing with one present.
    assert stages(
        [
            {
                "id": "fx_0123456789ab",
                "effect": "grain",
                "enabled": True,
                "parameters": {"strength": 4},
                "bindings": [],
            }
        ]
    )


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
            ":x=(iw-ow)/2+iw*0.01*sin(2*PI*0.123457*(t+0))"
            ":y=(ih-oh)/2+ih*0.01*cos(2*PI*0.169136*(t+0))"
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


def test_a_relative_data_root_is_anchored_before_a_look_reaches_the_chain(tmp_path, monkeypatch):
    r"""A2, and it is the one relative path the `cwd` fix left in the argv.

    A bound Shot's render runs with ffmpeg's working directory set to the export's `workdir` or
    to `previews/`, so a `sendcmd` script can be a bare relative filename (R-30). `lut3d=file=`
    is the only other filesystem reference the composed chain holds, and it is built from
    `discover_luts(settings.data_root)` — which under `MVP_DATA_ROOT=data` handed back
    `data/luts/warm-shift.cube`. Reproduced 2026-08-28 through this project's real ffmpeg: a
    Shot carrying **both** a binding and a Grade card gave rc -2 and
    `[Parsed_lut3d_6 @ ...] data/luts/warm-shift.cube: No such file or directory`, and the last
    line of the stderr the Director is shown blames the output file rather than the grade.

    `Settings` anchors it now (`config._anchor_data_root`), and the assertion is written where
    the fault was visible: the text of the chain. Anything relative in there but the script name
    is a file ffmpeg will look for in the wrong directory.
    """
    monkeypatch.chdir(tmp_path)
    settings = Settings(data_root="data")
    assert settings.data_root.is_absolute(), settings.data_root
    assert settings.data_root == (tmp_path / "data").resolve()

    looks = discover_luts(settings.data_root)
    warm = next(entry for entry in looks if entry.lut_id == "warm-shift")
    assert warm.path.is_absolute(), warm.path

    envelope = {"analysis_rate": 30.0, "band_count": 2, "bands": [[0.1] * 60, [0.2] * 60]}
    composed = build_effect_stages(
        [
            {"effect": "lut_look", "parameters": {"lut": "warm-shift"}},
            {
                "effect": "bloom",
                "parameters": {"intensity": 0.2},
                "bindings": [{"parameter": "intensity", "drive": "punch", "depth": 0.5}],
            },
        ],
        width=EXPORT_WIDTH,
        height=EXPORT_HEIGHT,
        luts=looks,
        envelope=envelope,
        shot_start=0.0,
        clip_seconds=2.0,
        shot_seconds=2.0,
    )
    text = ",".join([*composed.geometry, *composed.treatment])
    assert f"lut3d=file={lut_file_argument(warm.path)}:interp=tetrahedral" in text, text
    assert "file='data/luts" not in text, text
    # The script is the one deliberately relative reference, and it is the only one.
    assert len(composed.scripts) == 1
    assert f"sendcmd=f={composed.scripts[0].filename}" in text, text


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
        "slow_zoom": {"zoom": 1.5, "direction": "out"},
        "handheld_shake": {"amplitude": 0.03, "frequency": 3.5},
        "dutch_tilt": {"angle": -8.5},
        "mirror": {"axis": "both"},
        "grain": {"strength": 18, "seed": 12345},
        "vignette": {"angle": 0.9},
        "soft_focus": {"sigma": 3.5},
        "sharpen": {"amount": 1.25},
        "banding_suppression": {"threshold": 0.02},
        "bloom": {"intensity": 0.65, "threshold": 0.55, "radius": 9.5},
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
        "edge_treatment": {"strength": 0.6, "low": 0.12, "high": 0.35},
        "scanlines": {"strength": 0.45, "lines": 120},
        "pixel_shuffle": {"amount": 0.4, "block": 6, "seed": 4242},
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
            # A clip that begins a third of the way into a two-second Shot, so the two stages
            # that read the clock and the one that ramps over the Shot are all exercised away
            # from the zero every other test in this file composes at.
            clip_offset=0.75,
            shot_seconds=2.0,
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


def test_the_whole_catalogue_stacked_at_once_renders_through_the_real_chain(tmp_path: Path):
    """One render of a *combination*, because every other real render in this file is one
    effect — or grain and a vignette — and combinations are string-only otherwise.

    The whole catalogue at once is the extreme of the matrix: four families, both insertion
    points, three effects that compose to more than one stage, four branched filtergraphs
    sharing one comma-joined chain with twenty unbranched filters, and both of the stages that
    read the clip's place inside its Shot. It is also the only place the escaped comma inside
    Dutch Tilt's `max()` shares a chain with everything else that escapes one — Slow Zoom's
    `min()`, Bloom's `if(gt(...))` and Scanlines' two `max()`es.

    The frame grid is the assertion that matters: twenty-nine filters deep and four branches in,
    six frames in at the export's geometry must still be six frames out at the export's
    geometry. That is the claim `BRANCH_FRAME_GUARD` exists to keep, and without it this is
    exactly the render that comes back one frame short.
    """
    write_default_luts(lut_directory(tmp_path), size=5)
    luts = discover_luts(tmp_path)
    everything = [
        effect("punch_in", zoom=1.2),
        effect("slow_zoom", zoom=1.3, direction="in"),
        effect("handheld_shake", amplitude=0.02, frequency=3),
        effect("dutch_tilt", angle=-6),
        effect("mirror", axis="both"),
        effect("grain", strength=14, seed=99),
        effect("vignette", angle=0.7),
        effect("soft_focus", sigma=1.5),
        effect("sharpen", amount=0.8),
        effect("banding_suppression", threshold=0.01),
        effect("bloom", intensity=0.5, threshold=0.6, radius=6),
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
        effect("edge_treatment", strength=0.5, low=0.1, high=0.3),
        effect("scanlines", strength=0.35, lines=90),
        effect("pixel_shuffle", amount=0.3, block=6, seed=7),
    ]
    assert {spec["effect"] for spec in everything} == set(EFFECT_CATALOGUE)

    built = build_effect_stages(
        everything, width=320, height=240, luts=luts, clip_offset=1.25, shot_seconds=4.0
    )
    # Eight geometry stages before `scale` from five effects — Dutch Tilt is two of them, Mirror
    # on both axes is two more, and the first is the branch guard, which is a stage no effect
    # asked for — and twenty-one treatments from twenty effects, the extra being
    # Lift/Gamma/Gain's inseparable pair.
    assert built.geometry[0] == BRANCH_FRAME_GUARD
    assert len(built.geometry) == 8
    assert len(built.treatment) == 21
    # Four of those twenty-nine are filtergraphs rather than filters: Slow Zoom, Bloom, Edge
    # Treatment and Pixel Shuffle. Scanlines is the fifth new effect and is not one of them.
    branches = [stage for stage in (*built.geometry, *built.treatment) if ";" in stage]
    assert len(branches) == 4, branches

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


# ------------------------------------------------------------------------------------------
# The preview fingerprint (AD-23, AD-28).
#
# Nothing here asserts the digest against a literal. A hash pinned to a constant would fail on
# every wording change to the payload and prove nothing about the property that matters, which is
# that the fingerprint moves when — and only when — the picture would.
# ------------------------------------------------------------------------------------------


def a_fingerprint(**changed):
    """One preview fingerprint over a fixed baseline, with the named inputs moved."""
    inputs = {
        "take": "music-video-producer/project_x/shots/shot_a-h3_00001-audio.mp4",
        "window_start": 4.0,
        "window_duration": 4.0,
        "offset": 0.5,
        "stack": [{"effect": "grain", "enabled": True, "parameters": {"strength": 30.0}}],
        "bindings": (),
        "song_fingerprint": "4096-abcdef",
        "transition": None,
        "width": 528,
        "height": 304,
    }
    inputs.update(changed)
    return preview_fingerprint(**inputs)


def test_the_preview_fingerprint_names_its_eight_inputs_in_the_documented_order():
    """AD-28 fixes the list and the order. The names are the contract a later epic fills in.

    The fourth is `chain` and not `stack`: what is hashed there is the filter text the stack
    composes to, and the two are not the same thing — a stored stack is sparse, so a corrected
    default and a corrected composer both move the picture without moving a byte of it.
    """
    assert PREVIEW_FINGERPRINT_INPUTS == (
        "take",
        "window",
        "offset",
        "chain",
        "bindings",
        "song",
        "transition",
        "geometry",
    )


def test_every_one_of_the_eight_inputs_moves_the_fingerprint_on_its_own():
    """Each input moved alone produces a different name, so the cached clip it named is no
    longer the one that answers. This is the whole of staleness: no flag, a name that either
    matches or does not."""
    baseline = a_fingerprint()
    assert a_fingerprint() == baseline
    moved = {
        "take": {"take": "shots/shot_a-h3_00002-audio.mp4"},
        "window start": {"window_start": 4.5},
        "window duration": {"window_duration": 3.5},
        "offset": {"offset": 0.75},
        "stack": {
            "stack": [{"effect": "grain", "enabled": True, "parameters": {"strength": 31.0}}]
        },
        "bindings": {"bindings": [{"parameter": "grain.strength", "band": 2}]},
        "song fingerprint": {"song_fingerprint": "4096-fedcba"},
        "transition": {"transition": {"kind": "fade", "seconds": 0.5}},
        "preview width": {"width": 640},
        "preview height": {"height": 360},
    }
    seen = {baseline: "baseline"}
    for name, change in moved.items():
        digest = a_fingerprint(**change)
        assert digest not in seen, f"{name} collided with {seen.get(digest)}"
        seen[digest] = name


def test_the_bindings_and_transition_slots_are_hashed_before_anything_fills_them():
    """Epic 10 and Epic 11 fill two of the eight. They are hashed **now**, empty, so that
    filling them later moves the fingerprint of the Shots that acquire one rather than every
    Shot in every project at once — which is what leaving them out would cost on the day those
    epics merge. Their emptiness participates: an explicit empty is the default's answer, and a
    value is a different one."""
    assert a_fingerprint(bindings=(), transition=None) == a_fingerprint()
    assert a_fingerprint(bindings=[]) == a_fingerprint()
    assert a_fingerprint(bindings=[{"parameter": "grain.strength"}]) != a_fingerprint()
    assert a_fingerprint(transition={}) != a_fingerprint()


def test_a_card_id_reaches_neither_the_chain_nor_the_fingerprint():
    """R-33's cost, measured rather than asserted in prose: **adding `id` moved nothing.**

    A card id names the card, not the picture. It is not one of the eight inputs, it is not read
    by any composer, and `validate_stack` accepts it and ignores it -- so a Shot that gained an id
    (which is every Shot in every project, on its next save) composes the identical filter text,
    names the identical preview clip, and keeps every clip already cached under that name. That is
    R-20's empty-stack guarantee and AD-23's cache, both still standing after this epic.

    Asserted on the composed chain and on the name, never on an mp4 (R-20).
    """
    plain = [{"effect": "grain", "enabled": True, "parameters": {"strength": 30.0}}]
    identified = [dict(spec, id="fx_0123456789ab") for spec in plain]

    assert build_effect_stages(identified, width=1920, height=1080) == build_effect_stages(
        plain, width=1920, height=1080
    )
    assert a_fingerprint(stack=identified) == a_fingerprint(stack=plain)
    # And two cards of one effect that differ **only** by id are one picture, which is the
    # statement that would be false if an id had crept into the chain or the payload.
    assert a_fingerprint(stack=[dict(plain[0], id="fx_ffffffffffff")]) == a_fingerprint()


def test_two_states_that_compose_to_one_chain_fingerprint_alike():
    """`1` and `1.0` are one filter string, so they must be one look — a fingerprint that told
    them apart would re-render a preview that cannot possibly differ. Key order is not a
    contract either, and neither is how much of the look a manifest happens to spell out."""
    integral = a_fingerprint(
        stack=[{"effect": "contrast", "enabled": True, "parameters": {"amount": 1}}]
    )
    fractional = a_fingerprint(
        stack=[{"effect": "contrast", "enabled": True, "parameters": {"amount": 1.0}}]
    )
    reordered = a_fingerprint(
        stack=[{"parameters": {"amount": 1.0}, "enabled": True, "effect": "contrast"}]
    )
    # Every declared parameter written out, against the sparse spelling `stored_effect_stack`
    # actually writes. One chain, so one name: a fingerprint that told them apart would hand a
    # Director a re-render for a manifest that was tidied.
    spelled_out = a_fingerprint(
        stack=[{"effect": "grain", "enabled": True, "parameters": {"strength": 30.0, "seed": 0}}]
    )
    assert integral == fractional == reordered
    assert spelled_out == a_fingerprint()
    # And a difference a filter string cannot express is not a difference: `_number` formats to
    # six decimals, which is the resolution the chain itself has.
    assert a_fingerprint(offset=0.5) == a_fingerprint(offset=0.50000001)
    assert a_fingerprint(offset=0.5) != a_fingerprint(offset=0.5001)


def test_the_two_stage_groups_are_hashed_as_a_pair_and_not_run_together(monkeypatch):
    """`scale` sits between the geometry group and the treatment group (`EffectStages`), so the
    same stage on either side of it is not the same picture. The chain slot hashes the two
    groups as a pair for that reason, and a payload that ran them together would call these two
    chains one and serve either clip for both.

    Both composers are replaced, because no two entries in this catalogue compose the same text
    from different families and the case cannot otherwise be built — which is also why it is
    worth pinning: nothing today would notice if the pair became a concatenation.
    """
    geometry = EFFECT_CATALOGUE["mirror"]
    texture = EFFECT_CATALOGUE["grain"]
    stack = [
        {"effect": "mirror", "enabled": True, "parameters": {}},
        {"effect": "grain", "enabled": True, "parameters": {}},
    ]

    def composing(*stages):
        return lambda values, context: stages

    monkeypatch.setitem(
        EFFECT_CATALOGUE, "mirror", dataclasses.replace(geometry, compose=composing("hflip"))
    )
    monkeypatch.setitem(
        EFFECT_CATALOGUE, "grain", dataclasses.replace(texture, compose=composing("negate"))
    )
    assert stages(stack) == EffectStages(geometry=("hflip",), treatment=("negate",))
    before_the_scale = a_fingerprint(stack=stack)

    monkeypatch.setitem(
        EFFECT_CATALOGUE, "mirror", dataclasses.replace(geometry, compose=composing())
    )
    monkeypatch.setitem(
        EFFECT_CATALOGUE,
        "grain",
        dataclasses.replace(texture, compose=composing("hflip", "negate")),
    )
    assert stages(stack) == EffectStages(geometry=(), treatment=("hflip", "negate"))

    assert a_fingerprint(stack=stack) != before_the_scale


def test_a_corrected_composer_moves_the_fingerprint_of_a_stack_nobody_touched(monkeypatch):
    """The defect this slot exists to close, and it was live in shipped code: `e4aec46` moved
    Scanlines' grid origin from `x=-1` to `x=-t`, removing a black left-edge bar measured at
    **26 dark columns** on a white 1920x1080 frame at `lines=20`. Every preview already cached
    under the old spelling went on being served — for ever, because nothing in this application
    evicts `previews/` and no control clears it.

    No input the Director controls has moved here: the same card, the same values, a different
    picture. A name taken from the stack cannot express that. A name taken from the chain can,
    and the composer is *replaced* rather than edited so that the test is about the fingerprint
    rather than about Scanlines.
    """
    stack = [{"effect": "scanlines", "enabled": True, "parameters": {"strength": 0.5}}]
    shipped = a_fingerprint(stack=stack)
    definition = EFFECT_CATALOGUE["scanlines"]

    def one_pixel_origin(values, context):
        """The `7db970c` spelling, rebuilt from the shipped stage: `drawgrid=x=-1:…`."""
        return tuple(
            "drawgrid=x=-1:" + stage.split(":", 1)[1]
            for stage in definition.compose(values, context)
        )

    monkeypatch.setitem(
        EFFECT_CATALOGUE, "scanlines", dataclasses.replace(definition, compose=one_pixel_origin)
    )
    (patched,) = stages(stack).treatment
    assert patched.startswith("drawgrid=x=-1:y=0:")
    assert a_fingerprint(stack=stack) != shipped


def test_a_corrected_catalogue_default_moves_the_fingerprint_of_a_stack_nobody_touched(
    monkeypatch,
):
    """The other half, and the one `stored_effect_stack` argues for in its own words: a stack is
    stored sparsely so that *"a corrected default"* can reach the projects that would benefit
    from it. A name taken from the sparse spec cannot notice one — the manifest holds no
    `strength` to move. A name taken from the chain notices, because the default is in it."""
    stack = [{"effect": "grain", "enabled": True, "parameters": {}}]
    shipped = a_fingerprint(stack=stack)
    definition = EFFECT_CATALOGUE["grain"]

    monkeypatch.setitem(
        EFFECT_CATALOGUE,
        "grain",
        dataclasses.replace(
            definition,
            parameters=tuple(
                dataclasses.replace(parameter, default=12.0)
                if parameter.name == "strength"
                else parameter
                for parameter in definition.parameters
            ),
        ),
    )
    assert a_fingerprint(stack=stack) != shipped


def test_a_stack_the_validator_would_refuse_is_refused_here_in_the_catalogues_own_words():
    """A hand-edited manifest holding nested lists, `None`s or mixed key types used to be
    *named*, because the fingerprint hashed the spec and a spec always hashes. The chain is
    hashed now, so it refuses — with the catalogue's own sentence, never a `TypeError` out of
    the hasher. Nothing reaches it from the wire: the preview route composes the same chain from
    the same arguments first, and has answered 422 by the time a name is asked for.

    The two slots a later epic fills are **not** composed and go on accepting anything, which is
    what `_canonical` is for: Epic 10 and Epic 11 will put manifest values in them, and a
    fingerprint is not the place a hand-edited leftover may raise."""
    ugly = [
        {"effect": "grain", "parameters": {"strength": [1, 2, {"deep": None}]}},
        {"effect": "nonexistent", "parameters": {1: "one", "b": 2}},
        {},
    ]
    with pytest.raises(EffectRefusal):
        a_fingerprint(stack=ugly)
    # `bool` is an `int` in Python, and a flag where a number belongs is refused by the
    # validator rather than composed as `1`.
    with pytest.raises(EffectRefusal):
        a_fingerprint(stack=[{"effect": "contrast", "parameters": {"amount": True}}])

    leftovers = {"bindings": [{1: None, "b": [2, {"c": None}]}], "transition": {"x": [None]}}
    junk = a_fingerprint(**leftovers)
    assert len(junk) == 64
    assert junk == a_fingerprint(**leftovers)
    assert junk != a_fingerprint()


# ------------------------------------------------------------------------------------------
# Story 9.7 — the branch, the frame it costs, and the clip's place inside its Shot.
# ------------------------------------------------------------------------------------------


def test_a_branch_is_one_stage_and_a_whole_filtergraph():
    """The shape, pinned as text like every other chain in this application.

    One string, spliced into the comma-joined chain by a `trim_args` that knows nothing about
    it: three chains separated by `;`, the first ending on two labelled outputs so the comma
    that follows the whole thing belongs to the *last* chain, and exactly one unlabelled input
    and one unlabelled output left over — which is what makes it legal for `-vf`.
    """
    (branch,) = stages([effect("bloom", intensity=0.5, threshold=0.6, radius=6)]).treatment
    assert branch == (
        "split=2[fx0a][fx0b];"
        r"[fx0b]lutyuv=y=if(gt(val\,147)\,val\,0):u=0:v=0,gblur=sigma=6[fx0c];"
        "[fx0a][fx0c]blend=all_mode=screen:all_opacity=0.5"
    )
    # A comma-joined chain of one branch and one plain filter is still one `-vf` argument, and
    # the plain filter joins the branch's *last* chain rather than opening a fourth one.
    built = stages(
        [
            effect("bloom", intensity=0.5, threshold=0.6, radius=6),
            effect("vignette", angle=0.7),
        ]
    )
    assert ",".join(built.treatment).endswith(
        "[fx0a][fx0c]blend=all_mode=screen:all_opacity=0.5,vignette=angle=0.7"
    )


def test_two_branches_of_the_same_effect_do_not_share_a_label():
    """Nothing forbids two Blooms in one stack, and two branches named alike is an ffmpeg
    error rather than a different picture. The labels carry the effect's slot, so they cannot
    collide however many of one effect a Director stacks."""
    built = stages(
        [
            effect("bloom", intensity=0.4, radius=4),
            effect("bloom", intensity=0.8, radius=20),
        ]
    )
    assert len(built.treatment) == 2
    assert "[fx0a][fx0b]" in built.treatment[0]
    assert "[fx1a][fx1b]" in built.treatment[1]


def test_a_branchs_labels_are_counted_over_the_composed_order_and_not_the_stored_one():
    """AD-31, applied to the one thing that could have made storage order load-bearing again.

    The slot a branch names its links from is its position in the *composed* chain — family
    order first, the Director's order within a family — so a stack stored the other way round
    composes to the same text rather than to a graph whose labels ran backwards.
    """
    ordered = stages(
        [effect("bloom", intensity=0.5), effect("pixel_shuffle", amount=0.3, block=6, seed=1)]
    )
    stored_backwards = stages(
        [effect("pixel_shuffle", amount=0.3, block=6, seed=1), effect("bloom", intensity=0.5)]
    )
    assert ordered == stored_backwards
    # Texture is composed before Stylize, so Bloom is slot 0 in both.
    assert "[fx0a][fx0b]" in ordered.treatment[0]
    assert "[fx1a][fx1b]" in ordered.treatment[1]


def test_the_guard_arrives_with_the_first_branch_and_never_without_one():
    """It is a property of the chain rather than of any effect in it, so it is decided once
    over the finished groups — one framesync filter costs the chain one frame at its `fps`
    stage and four cost it the same one — and it goes at the head of the whole chain, which is
    the head of the geometry group whether or not any geometry effect is in the stack."""
    unbranched = stages([effect("grain", strength=8), effect("punch_in", zoom=1.2)])
    assert not unbranched.branched
    assert BRANCH_FRAME_GUARD not in unbranched.geometry

    treatment_only = stages([effect("bloom", intensity=0.5)])
    assert treatment_only.branched
    assert treatment_only.geometry == (BRANCH_FRAME_GUARD,)

    four = stages(
        [
            effect("slow_zoom", zoom=1.5),
            effect("bloom", intensity=0.5),
            effect("edge_treatment", strength=0.4),
            effect("pixel_shuffle", amount=0.3),
        ],
        shot_seconds=6.0,
    )
    assert four.geometry[0] == BRANCH_FRAME_GUARD
    assert len([stage for stage in (*four.geometry, *four.treatment) if ";" in stage]) == 4
    # One guard, not four.
    assert list(four.geometry).count(BRANCH_FRAME_GUARD) == 1


def test_the_branch_guard_is_the_frame_the_branch_would_otherwise_cost(tmp_path: Path):
    """The measurement, run rather than quoted — and the reason this story's riskiest change is
    the splice itself.

    Measured 2026-08-26 against this project's ffmpeg 7.0: a framesync filter — `blend`,
    `overlay`, every two-input filter there is — reports end-of-file at the **last frame's**
    presentation timestamp rather than one frame's duration past it, so the `fps` stage that
    closes every chain emits one frame fewer than it was handed, with `1 frames dropped` in
    ffmpeg's own accounting and **exit code zero**. Twenty-four frames in, twenty-three out, on
    a chain nothing reports a problem with. Every later Shot in the export would then sit one
    frame early against the song, and go on doing it for every branched Shot in the timeline.

    Both halves are asserted, because pinning only the fix would let the fix quietly become
    unnecessary and stay: without the guard the render really is short, and with it the count
    is exactly right. If a later ffmpeg stops dropping the frame, the first half of this fails
    and says so — and the guard is still harmless then, because `trim_args` always closes with
    `-frames:v`, which caps the count from above whichever way `fps` behaves.
    """
    source = tmp_path / "source.mp4"
    assert (
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1:r=24", "-frames:v", "24",
            "-pix_fmt", "yuv420p", str(source),
        ).returncode
        == 0
    )
    built = stages([effect("bloom", intensity=0.6, threshold=0.5, radius=6)])
    assert built.geometry == (BRANCH_FRAME_GUARD,)

    def render(name: str, geometry: tuple[str, ...]) -> tuple[int, int, int]:
        dest = tmp_path / f"{name}.mp4"
        result = subprocess.run(
            trim_args(
                source, dest, frames=24, width=320, height=240,
                geometry_stages=geometry, treatment_stages=built.treatment,
            ),
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr.strip()
        return frame_grid(dest)

    # The source holds exactly the frames asked for, which is the case the loss is visible in:
    # a take with frames to spare never reaches the end of the graph and never sees it.
    assert render("guarded", built.geometry) == (320, 240, 24)
    assert render("unguarded", ()) == (320, 240, 23)


def test_a_branch_at_its_identity_renders_the_frames_it_was_handed(tmp_path: Path):
    """The guard bought a frame back; this is the proof that it did not buy back a *different*
    frame, and that a branch is otherwise transparent.

    Bloom composed above every pixel in the source is the identity by construction: its leg
    finds no highlight, `screen(a, 0) = a` on all three planes, and the two copies recombine
    into the frame they were split from. So a branched chain — split, threshold, blur, blend,
    guard and all — must produce the **bit-identical** frames of a chain with no effects in it.
    Compared as frame checksums rather than as PSNR, because bit-identical is the claim; R-20
    forbids asserting on the encoded file, and `framemd5` reads the decoded frames.

    The clip is cut at an offset, because that is the case the compensating frame cannot be
    added at the *end* of the chain: `setpts=PTS-STARTPTS` zeroes every frame's duration, so a
    clone appended after the branch inherits the previous frame's timestamp and `fps` throws it
    away as a duplicate. Measured — it comes back one frame short with the guard at the end and
    exactly right with it at the front.
    """
    source = tmp_path / "source.mp4"
    # Clamped below Bloom's own maximum threshold, so nothing in the frame can bloom whatever
    # the blur does with it.
    assert (
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1:r=24",
            "-vf", r"lutyuv=y=clip(val\,16\,200)",
            "-frames:v", "24", "-pix_fmt", "yuv420p", str(source),
        ).returncode
        == 0
    )
    built = stages([effect("bloom", intensity=1.0, threshold=1.0, radius=8)])
    assert built.branched and built.geometry == (BRANCH_FRAME_GUARD,)

    def checksums(name: str, composed: EffectStages) -> list[str]:
        dest = tmp_path / f"{name}.mp4"
        result = subprocess.run(
            trim_args(
                source, dest, frames=18, width=320, height=240, offset=0.25,
                geometry_stages=composed.geometry, treatment_stages=composed.treatment,
            ),
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr.strip()
        digests = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", dest.as_posix(), "-f", "framemd5", "-"],
            capture_output=True, text=True, check=False,
        )
        assert digests.returncode == 0, digests.stderr.strip()
        return [
            line.rsplit(",", 1)[-1].strip()
            for line in digests.stdout.splitlines()
            if line and not line.startswith("#")
        ]

    plain = checksums("plain", EffectStages())
    branched = checksums("branched", built)
    assert len(plain) == 18
    assert branched == plain


def test_scanlines_draw_no_column_at_a_delivery_height_where_one_could_appear(tmp_path: Path):
    """The one measured fact `drawgrid` does not advertise: it always draws a *vertical* line
    too, and where it lands depends on a thickness that changes with the delivery.

    A grid line spans `[x, x+t-1]` — thickness runs **forward** from the origin — so an origin
    one pixel outside the picture hides the column only while `t` is 1, and `t` is
    `trunc(ih/lines/2)`, which is 1 only when the frame is shorter than four lines. That is why
    **this test renders at 1920x1080**: at 64x60, which is where it used to compose, `t` floors
    to 1 and the defect it claims to catch cannot appear. Measured on a white frame at the real
    delivery, sampling a row no scanline is on, with the origin at `-1`:

    | lines | thickness | dark columns |
    |---|---|---|
    | 200 (the catalogue default) | 2 | 1 |
    | 100 | 5 | 4 |
    | 40 | 13 | 12 |
    | 20 | 27 | **26** |

    So both halves are asserted at a height where the bar is real: no column of a non-scanline
    row is dark at either end of the `lines` range, and the rows are still there. A 26-pixel
    black bar down the left edge of every scanlined shot is what this is standing in front of,
    and it was shipping at the default.
    """
    source = tmp_path / "white.mp4"
    assert (
        ffmpeg(
            "-f", "lavfi", "-i", "color=c=white:s=1920x1080:d=1:r=24",
            "-frames:v", "2", "-pix_fmt", "yuv420p", str(source),
        ).returncode
        == 0
    )

    def scanlined(lines: int) -> np.ndarray:
        built = build_effect_stages(
            [effect("scanlines", strength=0.5, lines=lines)], width=1920, height=1080
        )
        assert built.treatment == (
            (
                rf"drawgrid=x=-max(1\,trunc(ih/{lines}/2)):y=0:w=iw*2"
                rf":h=max(2\,trunc(ih/{lines})):t=max(1\,trunc(ih/{lines}/2)):c=black@0.5"
            ),
        )
        assert not built.branched and built.geometry == ()
        dest = tmp_path / f"scanlined-{lines}.mp4"
        assert (
            subprocess.run(
                trim_args(
                    source, dest, frames=2, width=1920, height=1080,
                    treatment_stages=built.treatment,
                ),
                capture_output=True, check=False,
            ).returncode
            == 0
        )
        # The frame rule, on the effect that is not a branch: no guard, and the count unmoved.
        assert frame_grid(dest) == (1920, 1080, 2)
        raw = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-i", dest.as_posix(), "-frames:v", "1",
                "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
            ],
            capture_output=True, check=False,
        ).stdout
        return np.frombuffer(raw, dtype=np.uint8).reshape(1080, 1920, 3)

    # The catalogue's own default: a cell of 5 rows and a line of 2, so rows 0-1 are dark and
    # rows 2-4 are not. Sampled a whole row past the line, because the encode bleeds one row.
    frame = scanlined(200)
    middle = [int(value) for value in frame[:7, 960, 0]]
    assert middle[0] < 200 and middle[1] < 200, middle
    assert middle[3] > 240 and middle[4] > 240, middle
    assert middle[5] < 200 and middle[6] < 200, middle
    # And nothing vertical anywhere on that row. With the origin at `-1` this is `[0]`.
    assert [x for x in range(1920) if frame[3, x, 0] < 200] == []

    # The far end of the dial, where the bar was 26 pixels wide: a cell of 54 and a line of 27.
    frame = scanlined(20)
    assert int(frame[0, 960, 0]) < 200 and int(frame[26, 960, 0]) < 200
    assert int(frame[28, 960, 0]) > 240 and int(frame[52, 960, 0]) > 240
    assert [x for x in range(1920) if frame[28, x, 0] < 200] == []


def test_a_pinned_branch_at_a_zero_opacity_reproduces_its_input_exactly(tmp_path: Path):
    r"""`_branch_stage`'s stated invariant, met at the two composers it did not hold for.

    The untouched copy is only untouched if the graph never converts it, and `split` carries one
    pixel format for its input and both of its outputs — so `edgedetect`'s `gbrp` and
    `shufflepixels`' `yuv444p` decided the format of the pristine copy too, and because the stage
    upstream of every treatment branch is `scale`, which outputs whatever it is asked for, ffmpeg
    satisfied that by negotiating **`scale` itself** wide and converting both outputs of `split`
    back. Measured against the effect-free chain at a dial `_number` renders `"0"`:
    `edge_treatment` came back `y:43.21 u:35.67 v:33.24` and `pixel_shuffle` `y:49.40 u:36.84
    v:34.02` — on the half of the picture the branch exists to preserve, at a setting that
    renders as no effect at all.

    Written out as text first, because the pin is at **both** ends and neither alone moves the
    picture: measured, `format=yuv420p` on the leg's end only and before `split` only are each
    bit-for-bit as bad as no pin at all. Then rendered, because that is the only thing that can
    say whether the pristine copy came through — and compared as frame checksums rather than as
    PSNR, since bit-identical is the claim.

    `bloom` is here as the control: 4:2:0-native, unpinned, and already exact. If it ever stops
    being exact the pin is not optional any more, and this says so in the same breath.
    """
    assert BRANCH_LEG_FORMAT == "format=yuv420p"
    (edges,) = stages([effect("edge_treatment", strength=0.5, low=0.1, high=0.4)]).treatment
    assert edges == (
        "format=yuv420p,split=2[fx0a][fx0b];"
        "[fx0b]edgedetect=low=0.1:high=0.4:mode=colormix,format=yuv420p[fx0c];"
        "[fx0c][fx0a]blend=all_mode=normal:all_opacity=0.5"
    )
    (shuffled,) = stages([effect("pixel_shuffle", amount=0.3, block=6, seed=7)]).treatment
    assert shuffled == (
        "format=yuv420p,split=2[fx0a][fx0b];"
        "[fx0b]shufflepixels=mode=block:width=6:height=6:seed=7,format=yuv420p[fx0c];"
        "[fx0c][fx0a]blend=all_mode=normal:all_opacity=0.3"
    )
    # The two that need no pin compose exactly the text they always composed.
    (bloomed,) = stages([effect("bloom", intensity=0.5, threshold=0.6, radius=6)]).treatment
    assert bloomed.startswith("split=2[fx0a][fx0b];") and BRANCH_LEG_FORMAT not in bloomed
    (zoomed,) = stages([effect("slow_zoom", zoom=1.4)], shot_seconds=8.0).geometry[1:]
    assert zoomed.startswith("split=2[fx0a][fx0b];") and BRANCH_LEG_FORMAT not in zoomed

    source = tmp_path / "source.mp4"
    assert (
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1:r=24", "-frames:v", "24",
            "-pix_fmt", "yuv420p", str(source),
        ).returncode
        == 0
    )

    def checksums(name: str, composed: EffectStages) -> list[str]:
        dest = tmp_path / f"{name}.mp4"
        result = subprocess.run(
            trim_args(
                source, dest, frames=24, width=320, height=240,
                geometry_stages=composed.geometry, treatment_stages=composed.treatment,
            ),
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr.strip()
        assert frame_grid(dest) == (320, 240, 24), name
        digests = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", dest.as_posix(), "-f", "framemd5", "-"],
            capture_output=True, text=True, check=False,
        )
        assert digests.returncode == 0, digests.stderr.strip()
        return [
            line.rsplit(",", 1)[-1].strip()
            for line in digests.stdout.splitlines()
            if line and not line.startswith("#")
        ]

    plain = checksums("plain", EffectStages())
    assert len(plain) == 24
    # A dial the validator accepts and `_number` writes as `0`: the branch is composed, the
    # opacity is zero, and the picture that comes out must be the picture that went in. It is
    # also the case the old graph degraded *most*, because nothing of the treated copy is mixed
    # in to hide the round trip the pristine one was taking.
    for effect_id, parameters in (
        ("edge_treatment", {"strength": 1e-7}),
        ("pixel_shuffle", {"amount": 1e-7}),
        ("bloom", {"intensity": 1e-7}),
    ):
        built = build_effect_stages(
            [{"effect": effect_id, "parameters": parameters}], width=320, height=240
        )
        assert built.branched and built.geometry == (BRANCH_FRAME_GUARD,), effect_id
        assert built.treatment[0].endswith("all_opacity=0"), built.treatment[0]
        assert checksums(effect_id, built) == plain, effect_id


def test_a_semicolon_in_a_looks_filename_is_not_a_branch_and_draws_no_guard(tmp_path: Path):
    """`EffectStages.branched` decides whether the frame guard is emitted, so what counts as a
    branch has to be exactly what a branch is.

    The old test was `";" in stage`, argued exact because "no filter option this catalogue writes
    contains one" — which `lut_file_argument` contradicts in its own docstring, where surviving
    "spaces, commas, semicolons, brackets" is the entire point of the quoting. A Director who
    drops `warm;cool.cube` in the looks folder composes a `lut3d` stage holding a semicolon with
    no `split=` anywhere in it, and a linear chain was getting `BRANCH_FRAME_GUARD`.

    Both halves are rendered, because the consequence is a frame count and nothing else: the
    guard clones a frame onto the end and the `fps` stage drops one only when a framesync filter
    swallowed one first. On a take that covers its window the two cancel and nothing is visible;
    on a take that does not, the clone is kept. Twelve frames of source asked for twenty-four
    come back as **twelve** on the chain as composed, and **thirteen** with the guard the old
    predicate would have prepended — a frame of picture the take never held.
    """
    directory = lut_directory(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "warm;cool.cube").write_text(
        cube_text(17, identity_transform), encoding="utf-8"
    )
    luts = discover_luts(tmp_path)
    assert [entry.path.name for entry in luts] == ["warm;cool.cube"]

    built = build_effect_stages(
        [effect("lut_look", lut=luts[0].lut_id)], width=320, height=240, luts=luts
    )
    (stage,) = built.treatment
    assert ";" in stage and "split=" not in stage
    assert not built.branched
    assert built.geometry == ()
    # A real branch in the same stack is still a branch, so this is a narrower test and not a
    # broken one.
    with_bloom = build_effect_stages(
        [effect("lut_look", lut=luts[0].lut_id), effect("bloom", intensity=0.5)],
        width=320, height=240, luts=luts,
    )
    assert with_bloom.branched and with_bloom.geometry == (BRANCH_FRAME_GUARD,)

    source = tmp_path / "short.mp4"
    assert (
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1:r=24", "-frames:v", "12",
            "-pix_fmt", "yuv420p", str(source),
        ).returncode
        == 0
    )

    def frames(name: str, geometry: tuple[str, ...]) -> int:
        dest = tmp_path / f"{name}.mp4"
        result = subprocess.run(
            trim_args(
                source, dest, frames=24, width=320, height=240,
                geometry_stages=geometry, treatment_stages=built.treatment,
            ),
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr.strip()
        return frame_grid(dest)[2]

    assert frames("graded", built.geometry) == 12
    assert frames("guarded", (BRANCH_FRAME_GUARD, *built.geometry)) == 13


def test_slow_zoom_refuses_a_span_that_composes_as_zero_however_positive_it_is():
    r"""The guard is on the text, because the text is the denominator.

    `_number` renders anything below 5e-7 as `"0"`, so a span that passed a `> 0` check composed
    `min((t+0)/0\,1)` — and ffmpeg does not refuse that: measured at 1e-9, 4.9e-7 and 5e-7, all
    three rendered `rc=0`, twenty-four frames, correct dimensions, which is precisely what the
    refusal's own sentence predicts will not happen. `Shot.duration` is only `Field(gt=0)` and
    the preview route hands it through with no window check of its own, so the span is reachable;
    the export refuses a sub-frame window before this is asked.

    The refusal sentence is the one that was already there, word for word — a second wording of
    an existing refusal is the one thing this surface may not grow.
    """
    sentence = (
        "'slow_zoom' ramps over its Shot's own length, and no length was given to compose it "
        "against. Nothing was composed."
    )
    for span in (0.0, -1.0, 1e-9, 4.9e-7, 5e-7):
        with pytest.raises(EffectRefusal) as refusal:
            stages([effect("slow_zoom", zoom=1.5)], shot_seconds=span)
        assert str(refusal.value) == sentence, span

    # And the first span that survives `_number` composes, with that text as the denominator:
    # the guard refuses what would be written as zero and nothing more.
    built = stages([effect("slow_zoom", zoom=1.5)], shot_seconds=1e-6)
    assert r"min((t+0)/0.000001\,1)" in built.geometry[1]


def test_the_recorded_look_names_only_the_effects_that_composed_a_stage():
    """FX-25's record, held to its own docstring: *"for the effects that actually composed a
    stage"*, and *"a record naming one would describe a picture the export did not produce"*.

    Every effect in the catalogue has a value that means "leave it alone" and composes to no
    filter at all, and since story 9.7 all five of the newest ones **default** to it — so a
    Director who added a Bloom card and left it alone put `bloom:{"intensity":0,...}` in the job
    record of an export that never ran a bloom. Two ways to compose nothing, one rule: the
    disabled card was already omitted, and the one sitting at its identity now is too.

    The identity is the composer's own answer rather than a table repeated here, which is why
    `chroma_split` is in this test: its identity is the *pixels* the shift becomes, so the same
    stored fraction composes a stage at one delivery and nothing at another, and the record
    follows the width it is given.
    """
    assert exported_look([effect("bloom")]) == ()
    assert exported_look([effect("bloom", intensity=0.0, threshold=0.9)]) == ()
    assert exported_look([effect("slow_zoom"), effect("scanlines"), effect("pixel_shuffle")]) == ()
    # Enabled and off the identity: recorded, with every resolved value, in chain order.
    assert exported_look([effect("scanlines", strength=0.4), effect("punch_in", zoom=1.2)]) == (
        'punch_in:{"zoom":1.2}',
        'scanlines:{"lines":200,"strength":0.4}',
    )
    # The rule that was already there, unmoved: a disabled card composes nothing either.
    assert exported_look([effect("bloom", enabled=False, intensity=0.8)]) == ()

    # A shift of a five-hundredth of a percent is half a pixel at 1056 wide and rounds to none,
    # so no `chromashift` is composed and none is recorded; the same look at 1920 moves a whole
    # pixel and is. The record follows the delivery because the chain does.
    hairline = [effect("chroma_split", shift=0.0004)]
    assert exported_look(hairline, width=1056, height=608) == ()
    assert exported_look(hairline, width=1920, height=1080) == (
        'chroma_split:{"shift":0.0004}',
    )
    assert build_effect_stages(hairline, width=1056, height=608).treatment == ()
    assert build_effect_stages(hairline, width=1920, height=1080).treatment == (
        "chromashift=cbh=1:crh=-1",
    )


def test_slow_zoom_ramps_across_a_seam_as_one_move_and_never_samples_outside(tmp_path: Path):
    """The effect that needed the Shot's own length, and the reason `shot_seconds` is the
    **Shot's** window rather than the clip's.

    A ramp measured against the clip would restart at the seam, which is the defect the pairing
    exists to remove; measured against the Shot, the second clip picks it up where the first one
    left it. Both are written out below at a seam five seconds into an eight-second Shot: the
    same denominator, and only the numerator moved.

    The scale factor never goes below 1 in either direction, which is FX-11's bound as
    arithmetic — a factor under 1 would show the frame's own edge — and the real render at the
    end is the frame rule: the branch changes the picture, not the grid it is on.
    """
    first = stages(
        [effect("slow_zoom", zoom=1.4, direction="in")], clip_offset=0.0, shot_seconds=8.0
    )
    second = stages(
        [effect("slow_zoom", zoom=1.4, direction="in")], clip_offset=5.0, shot_seconds=8.0
    )
    assert first.geometry[1] == (
        "split=2[fx0a][fx0b];"
        r"[fx0b]scale=w=trunc(iw*(1+0.4*min((t+0)/8\,1))/2)*2"
        r":h=trunc(ih*(1+0.4*min((t+0)/8\,1))/2)*2:eval=frame[fx0c];"
        "[fx0a][fx0c]overlay=x=(W-w)/2:y=(H-h)/2:eval=frame"
    )
    assert second.geometry[1] == first.geometry[1].replace("(t+0)", "(t+5)")

    # Zooming out starts at the target and returns to 1, so the factor is still never below it.
    out = stages(
        [effect("slow_zoom", zoom=1.4, direction="out")], clip_offset=0.0, shot_seconds=8.0
    )
    assert r"trunc(iw*(1.4-0.4*min((t+0)/8\,1))/2)*2" in out.geometry[1]

    # No span, no ramp: refused by name rather than dividing by a length nobody gave it.
    with pytest.raises(EffectRefusal) as refusal:
        stages([effect("slow_zoom", zoom=1.4)])
    assert str(refusal.value) == (
        "'slow_zoom' ramps over its Shot's own length, and no length was given to compose it "
        "against. Nothing was composed."
    )

    # And it really zooms, measured on the picture: a white square on black grows with the ramp
    # and the frame it grows inside does not.
    source = tmp_path / "square.mp4"
    assert (
        ffmpeg(
            "-f", "lavfi", "-i", "color=c=black:s=160x120:d=2:r=24",
            "-vf", "drawbox=x=60:y=40:w=40:h=40:color=white:t=fill",
            "-frames:v", "48", "-pix_fmt", "yuv420p", str(source),
        ).returncode
        == 0
    )
    built = stages(
        [effect("slow_zoom", zoom=2.0, direction="in")], clip_offset=0.0, shot_seconds=2.0
    )
    dest = tmp_path / "zoomed.mp4"
    assert (
        subprocess.run(
            trim_args(
                source, dest, frames=48, width=160, height=120,
                geometry_stages=built.geometry, treatment_stages=built.treatment,
            ),
            capture_output=True, check=False,
        ).returncode
        == 0
    )
    assert frame_grid(dest) == (160, 120, 48)
    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", dest.as_posix(), "-frames:v", "48",
            "-pix_fmt", "gray", "-f", "rawvideo", "-",
        ],
        capture_output=True, check=False,
    ).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 120, 160)
    white = [int((frame > 200).sum()) for frame in frames]
    # The square starts its own size and ends near four times it, because area goes as the
    # square of a zoom that reaches 2. Monotone throughout: one move, not a stutter.
    assert 1500 <= white[0] <= 1700
    assert white[-1] > 3 * white[0]
    assert all(later >= earlier - 8 for earlier, later in itertools.pairwise(white))


# ------------------------------------------------------------------------------------------
# The grid a look is composed for, against the grid its numbers were written for.
# ------------------------------------------------------------------------------------------


#: The five parameters in this catalogue denominated in **pixels** rather than in a fraction of
#: anything, written out by hand rather than derived from the catalogue: a table that grew a sixth
#: pixel-denominated parameter and did not scale it must fail here, and it cannot if this list is
#: computed from the same table the code reads.
#:
#: Each row is the effect, the stack that exercises it, the stage at 1920 wide, and the stage the
#: same stored numbers must compose to on a 960-wide grid. Sharpen's scaled number is its
#: **matrix** and not its parameter — `amount` is a strength and means the same at any size.
PIXEL_DENOMINATED = (
    (
        "soft_focus",
        {"sigma": 8},
        "gblur=sigma=8",
        "gblur=sigma=4",
    ),
    (
        "sharpen",
        {"amount": 1.5},
        "unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount=1.5",
        "unsharp=luma_msize_x=3:luma_msize_y=3:luma_amount=1.5",
    ),
    (
        "bloom",
        {"intensity": 0.5, "threshold": 0.7, "radius": 40},
        (
            "split=2[fx0a][fx0b];[fx0b]lutyuv=y=if(gt(val\\,169)\\,val\\,0):u=0:v=0,"
            "gblur=sigma=40[fx0c];[fx0a][fx0c]blend=all_mode=screen:all_opacity=0.5"
        ),
        (
            "split=2[fx0a][fx0b];[fx0b]lutyuv=y=if(gt(val\\,169)\\,val\\,0):u=0:v=0,"
            "gblur=sigma=20[fx0c];[fx0a][fx0c]blend=all_mode=screen:all_opacity=0.5"
        ),
    ),
    (
        "pixelate",
        {"size": 32},
        "pixelize=w=32:h=32:mode=avg",
        "pixelize=w=16:h=16:mode=avg",
    ),
    (
        "pixel_shuffle",
        {"amount": 0.5, "block": 32, "seed": 9},
        (
            "format=yuv420p,split=2[fx0a][fx0b];[fx0b]shufflepixels=mode=block"
            ":width=32:height=32:seed=9,format=yuv420p[fx0c];"
            "[fx0c][fx0a]blend=all_mode=normal:all_opacity=0.5"
        ),
        (
            "format=yuv420p,split=2[fx0a][fx0b];[fx0b]shufflepixels=mode=block"
            ":width=16:height=16:seed=9,format=yuv420p[fx0c];"
            "[fx0c][fx0a]blend=all_mode=normal:all_opacity=0.5"
        ),
    ),
)


def test_a_parameter_denominated_in_pixels_is_scaled_to_the_grid_it_is_composed_for():
    """Story 9.2 promises the preview is the export's picture "differing in nothing else", and
    for these five it was not: they are counts of pixels, the preview composes at half the
    export's grid, and a count of pixels covers twice as much of a frame half the size.

    The expectations are written out by hand, not halved by arithmetic here, because a test that
    scaled its own expectation would agree with a composer that scaled the wrong way.
    """
    for effect_id, parameters, at_export, at_preview in PIXEL_DENOMINATED:
        stack = [effect(effect_id, **parameters)]
        export = build_effect_stages(stack, width=1920, height=1080, shot_seconds=4.0)
        preview = build_effect_stages(
            stack, width=960, height=540, reference_width=1920, shot_seconds=4.0
        )
        assert export.treatment[-1] == at_export, effect_id
        assert preview.treatment[-1] == at_preview, effect_id
        # And the preview composed *without* the reference is the export's own text at half the
        # size, which is the defect stated as a test: the number buys nothing on its own.
        unaware = build_effect_stages(stack, width=960, height=540, shot_seconds=4.0)
        assert unaware.treatment[-1] == at_export, effect_id


def test_the_grid_a_look_was_written_for_composes_the_text_it_always_composed():
    """The export's argv may not move, which is the constraint the whole scaling is built around.

    A reference that is absent, zero, or this very grid all mean "these numbers are already the
    right ones", and every one of them has to be the identity — not a float that rounds to it,
    because `round(32 * 1.0)` and `int(32)` are the same number while `_number(8 * 1.0000001)`
    and `_number(8)` are not the same string.
    """
    for effect_id, parameters, at_export, _ in PIXEL_DENOMINATED:
        stack = [effect(effect_id, **parameters)]
        for width, height in ((1920, 1080), (1056, 608), (640, 360)):
            plain = build_effect_stages(stack, width=width, height=height, shot_seconds=4.0)
            for reference in (0, width):
                same = build_effect_stages(
                    stack, width=width, height=height,
                    reference_width=reference, shot_seconds=4.0,
                )
                assert same == plain, (effect_id, width, reference)
        assert build_effect_stages(
            stack, width=1920, height=1080, reference_width=1920, shot_seconds=4.0
        ).treatment[-1] == at_export, effect_id

    # A context that names no reference scales nothing, whatever grid it describes — pinned on the
    # dataclass itself because `exported_look` builds its probe context by hand and would be the
    # only thing to notice a default that had moved, and only for an effect that composed away.
    for width, height in ((1920, 1080), (1056, 608), (LOOK_PROBE_WIDTH, LOOK_PROBE_HEIGHT)):
        assert StageContext(width=width, height=height).pixel_scale == 1.0
        assert StageContext(width=width, height=height, reference_width=width).pixel_scale == 1.0


def test_every_other_effect_composes_the_same_text_at_every_grid():
    """The scaling reaches the five and nothing else. Twenty of the twenty-five are a fraction, a
    ratio of `iw`/`ih`, an angle or a luma code, and every one of them must be untouched by the
    grid it is composed for — `chroma_split` excepted, which reads the width because it stores a
    fraction and is the pattern the five are now following.
    """
    scaled = {name for name, *_ in PIXEL_DENOMINATED} | {"chroma_split"}
    exercised = {
        "punch_in": {"zoom": 1.4},
        "slow_zoom": {"zoom": 1.5, "direction": "out"},
        "handheld_shake": {"amplitude": 0.03, "frequency": 3.5},
        "dutch_tilt": {"angle": -8.5},
        "mirror": {"axis": "both"},
        "grain": {"strength": 18, "seed": 12345},
        "vignette": {"angle": 0.9},
        "banding_suppression": {"threshold": 0.02},
        "exposure": {"amount": 0.2},
        "contrast": {"amount": 1.6},
        "saturation": {"amount": 0.4},
        "temperature": {"amount": -0.35},
        "tint": {"amount": 0.3},
        "lift_gamma_gain": {"lift": 0.05, "gamma": 1.4, "gain": -0.1},
        "monochrome": {"amount": 0.75},
        "posterize": {"levels": 6},
        "edge_treatment": {"strength": 0.6, "low": 0.12, "high": 0.35},
        "scanlines": {"strength": 0.45, "lines": 120},
    }
    assert set(exercised) | scaled | {"lut_look"} == set(EFFECT_CATALOGUE)
    for effect_id, parameters in exercised.items():
        stack = [effect(effect_id, **parameters)]
        export = build_effect_stages(stack, width=1920, height=1080, shot_seconds=4.0)
        preview = build_effect_stages(
            stack, width=960, height=540, reference_width=1920, shot_seconds=4.0
        )
        assert export == preview, effect_id


def test_the_look_record_is_the_same_effects_however_the_preview_composes_them():
    """`exported_look` probes at a delivery larger than anything this application produces and
    names no reference, so the scaling cannot reach it. Checked because the record drops an
    effect that composes no stage, and a scaled block that falls to a single pixel does exactly
    that — on the *preview's* chain, which is not what a job record describes."""
    for effect_id, parameters, *_ in PIXEL_DENOMINATED:
        stack = [effect(effect_id, **parameters)]
        assert [entry.split(":", 1)[0] for entry in exported_look(stack)] == [effect_id]
    # The one case where a scaled block really does compose nothing, and it stays out of the
    # preview's chain rather than out of the record: a two-pixel block on a half-size grid is one
    # pixel, and `pixelize=w=1:h=1` changes no pixel at all.
    smallest = [effect("pixelate", size=2)]
    assert build_effect_stages(smallest, width=1920, height=1080).treatment == (
        "pixelize=w=2:h=2:mode=avg",
    )
    assert build_effect_stages(
        smallest, width=960, height=540, reference_width=1920
    ).treatment == ()
    assert [entry.split(":", 1)[0] for entry in exported_look(smallest)] == ["pixelate"]


def test_a_pixelated_frame_carries_the_same_blocks_across_at_both_delivery_sizes(tmp_path: Path):
    """The finding's own measurement, run as a test: one stack, two geometries, blocks counted.

    This is the check the epic did not have. The only test driving a chain through the preview's
    half geometry used `slow_zoom`, every term of which is a ratio of `iw`/`ih` — structurally
    unable to see a parameter denominated in pixels — so five effects shipped rendering at twice
    their relative size in the Monitor with nothing able to notice.

    Counted off the real picture rather than off the stage text, because the stage text was
    byte-identical at both geometries and *that was the defect*. The frame count and the frame
    size are asserted at both, because a treatment may not resize a frame and may not drop one:
    the assembled video matches the song within one frame whatever the look is.
    """
    export = (640, 360)
    preview = (320, 180)
    # Every column a different luma, so a block quantiser leaves runs a count can find.
    sources = {}
    for width, height in (export, preview):
        source = tmp_path / f"cells-{width}.mp4"
        assert (
            ffmpeg(
                "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d=1:r=24",
                "-frames:v", "6", "-vf", "geq=lum=mod(X*3+Y*5\\,255):cb=128:cr=128",
                "-pix_fmt", "yuv420p", str(source),
            ).returncode
            == 0
        ), width
        sources[(width, height)] = source

    def block_width(clip: Path, width: int, height: int) -> int:
        """How many pixels wide one block of the rendered picture is.

        The commonest run of equal luma along a row, rather than a count of the runs or their
        divisor. Neither of those survives the encode: two neighbouring blocks whose averages
        land on the same code read as one run of two blocks (two of the forty do at 640 wide),
        and x264 at draft CRF splits a few blocks into pieces at 320. The mode is unmoved by
        both — 38 runs of 16 at 640, 36 of 8 at 320, measured 2026-08-26.
        """
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", clip.as_posix(), "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True, check=False,
        ).stdout
        row = np.frombuffer(raw, dtype=np.uint8).reshape(height, width)[height // 2]
        edges = [0, *(1 + np.flatnonzero(row[1:] != row[:-1])).tolist(), width]
        lengths = [right - left for left, right in itertools.pairwise(edges)]
        return max(set(lengths), key=lengths.count)

    counted = {}
    for width, height in (export, preview):
        built = build_effect_stages(
            [effect("pixelate", size=16)],
            width=width, height=height, reference_width=export[0], shot_seconds=4.0,
        )
        dest = tmp_path / f"pixelated-{width}.mp4"
        assert (
            subprocess.run(
                trim_args(
                    sources[(width, height)], dest, frames=6, width=width, height=height,
                    geometry_stages=built.geometry, treatment_stages=built.treatment,
                ),
                capture_output=True, check=False,
            ).returncode
            == 0
        ), width
        assert frame_grid(dest) == (width, height, 6), width
        counted[width] = block_width(dest, width, height)

    # Sixteen pixels of the export's own frame, and eight of a frame half as wide — which is the
    # same block either time: forty across. Before this the preview drew twenty, because sixteen
    # pixels is twice as wide a share of a 320-wide frame.
    assert counted[export[0]] == 16
    assert counted[preview[0]] == 8
    assert export[0] / counted[export[0]] == preview[0] / counted[preview[0]] == 40
# ------------------------------------------------------------------------------------------
# Slice E2: the fingerprint learns the envelope, and the record learns what was driving.
# ------------------------------------------------------------------------------------------

#: Three preview fingerprints computed by `effects.py` **as it stood at `ad67a14`**, the commit
#: before this slice. Taken by importing that revision's file directly -- which is possible only
#: because it imports stdlib and nothing else, and is the sharpest available demonstration of why
#: that house rule is worth keeping.
#:
#: They are here for one claim and it is the claim this slice could most easily have broken
#: silently: **a Shot with no binding names the clip it already named.** Nothing evicts
#: `previews/`, so a payload that moved would have re-rendered every Shot in every project on this
#: machine, once, for pictures that did not change -- and every one of them would still have
#: looked right. `envelope`, `shot_start` and `clip_seconds` are new arguments to this function
#: and the fifth slot now has a real value to carry; none of that may reach an unbound stack.
FINGERPRINTS_BEFORE_BINDINGS = {
    "grain": "7ebe3bd59abb0c43f994cf09f5a52686859422f2a882e6b210d430540b8389ae",
    "bloom": "f2546cac152b4220f6c3e5f29b9176b03d534218a573277f27aec2bcf64c46e0",
    "empty": "6f1076ab2e60ad7a4d5d9c7eed289cbadc5f7773d29fcc04abfcbf67b70cea94",
}


def _unbound_fingerprint(stack, **changed):
    inputs = {
        "take": "shots/shot_a-h3_00001-audio.mp4",
        "window_start": 4.0,
        "window_duration": 3.0,
        "offset": 0.25,
        "stack": stack,
        "bindings": (),
        "song_fingerprint": "4096-abcdef",
        "transition": None,
        "width": 528,
        "height": 304,
    }
    inputs.update(changed)
    return preview_fingerprint(**inputs)


def test_a_shot_with_no_binding_names_the_clip_it_named_before_this_slice():
    """R-20 and constraint 7, pinned against the previous commit's own arithmetic.

    Asserted three ways, because the failure has three doors: an empty stack, a stack whose one
    card is linear, and a stack whose one card composes a branch. And asserted **again** with the
    three new arguments supplied, since a caller that has an envelope in its hand still must not
    move the name of a Shot that is not listening to it.
    """
    envelope = {"analysis_rate": 30.0, "band_count": 2, "bands": [[0.9] * 90, [0.2] * 90]}
    for name, stack in (
        ("empty", []),
        ("grain", [{"effect": "grain", "enabled": True, "parameters": {"strength": 30.0}}]),
        ("bloom", [{"effect": "bloom", "enabled": True, "parameters": {"intensity": 0.4}}]),
    ):
        assert _unbound_fingerprint(stack) == FINGERPRINTS_BEFORE_BINDINGS[name], name
        assert _unbound_fingerprint(
            stack, envelope=envelope, shot_start=4.0, clip_seconds=3.0
        ) == FINGERPRINTS_BEFORE_BINDINGS[name], name


def test_a_bound_shots_fingerprint_moves_with_the_measurement_that_drives_it():
    """The fifth slot and the fourth, and neither is redundant.

    The chain carries the compiled script's *name*, whose digest is taken over the script's own
    text -- so a re-measured envelope and a moved Shot both change the name of the clip even
    though the Director changed nothing. The `bindings` slot carries the Director's own numbers,
    which is the one thing the chain cannot see when two states compile to identical text.

    A `sendcmd` that resolves to nothing is silent, so the only defence against serving a clip
    from before a binding is that the name is different. This is that defence.
    """
    # A band with a **transient** in it, because `punch` measures level above its own running
    # average: a flat envelope at 0.9 and a flat one at 0.4 drive the identical nothing, compile
    # the identical script, and would make every assertion below pass for the wrong reason.
    def bumped(at: int):
        bass = [0.1] * 90
        for tick in range(at, at + 4):
            bass[tick] = 0.95
        return {"analysis_rate": 30.0, "band_count": 2, "bands": [bass, [0.2] * 90]}

    envelope = bumped(20)
    remeasured = bumped(40)
    stack = [{
        "effect": "bloom",
        "enabled": True,
        "parameters": {"intensity": 0.4},
        "bindings": [{"parameter": "intensity", "drive": "punch", "depth": 0.5}],
    }]
    bound = {
        "stack": stack,
        "bindings": (({"parameter": "intensity", "drive": "punch", "depth": 0.5},),),
        "envelope": envelope,
        "shot_start": 0.0,
        "clip_seconds": 3.0,
    }

    baseline = _unbound_fingerprint(**bound)
    assert baseline != FINGERPRINTS_BEFORE_BINDINGS["bloom"], (
        "a bound Shot named the same clip as the unbound one, so the cache would serve the "
        "undriven picture for ever"
    )
    assert _unbound_fingerprint(**bound) == baseline

    # The envelope moved: same binding, same Shot, a song measured again.
    assert _unbound_fingerprint(**{**bound, "envelope": remeasured}) != baseline
    # The Shot moved along the song, so it is driven by a different stretch of one measurement.
    assert _unbound_fingerprint(**{**bound, "shot_start": 0.5}) != baseline
    # The Director's own numbers moved.
    deeper = [{**stack[0],
               "bindings": [{"parameter": "intensity", "drive": "punch", "depth": 0.9}]}]
    assert _unbound_fingerprint(**{
        **bound,
        "stack": deeper,
        "bindings": (({"parameter": "intensity", "drive": "punch", "depth": 0.9},),),
    }) != baseline


def test_a_bound_stack_with_no_envelope_refuses_rather_than_naming_an_undriven_picture():
    """`build_effect_stages` refuses by name, and `preview_fingerprint` refuses with it.

    The alternative is the whole reason this epic is dangerous: composing the stage without its
    `sendcmd`, naming the clip after the bound state, and rendering the undriven picture into it
    at rc 0 with nothing to see. FX-15 read the only way it can be read here -- a binding is never
    silently dropped.
    """
    stack = [{
        "effect": "bloom",
        "enabled": True,
        "parameters": {"intensity": 0.4},
        "bindings": [{"parameter": "intensity", "drive": "punch", "depth": 0.5}],
    }]
    with pytest.raises(EffectRefusal) as refusal:
        _unbound_fingerprint(stack)
    assert str(refusal.value) == BINDING_NO_ENVELOPE_REFUSAL.format(
        effect="bloom", parameter="intensity"
    )


def test_the_export_record_names_every_binding_that_drove_it_with_its_settings_filled_in():
    """`exported_bindings`, `exported_look`'s sibling: what was *moving* while the look was
    applied. Two identical `effects` lists with different drives are two different pictures, and
    the record has to be able to tell them apart.

    Resolved rather than stored: the manifest's copy is sparse by design, so a record taken from
    it would stop meaning anything the day a default moved. Ordered by `FAMILY_ORDER` then by the
    Director's order within a family, which is the order the scripts sat in. A disabled card
    composed no stage, so nothing addressed it and it is not listed.
    """
    stack = [
        {"effect": "exposure", "parameters": {"amount": 0.2},
         "bindings": [{"parameter": "amount", "drive": "sustain", "depth": 0.5, "floor": 0.3}]},
        {"effect": "soft_focus", "parameters": {"sigma": 4},
         "bindings": [{"parameter": "sigma", "drive": "punch", "depth": 2}]},
        {"effect": "contrast", "parameters": {},
         "bindings": [{"parameter": "amount", "drive": "punch", "depth": 1}],
         "enabled": False},
    ]

    assert exported_bindings(stack) == (
        # Texture before Grade, which is `FAMILY_ORDER` and therefore the chain's own order --
        # never the order these were written in.
        (
            'soft_focus.sigma:{"band_centre":0.25,"band_softness":0.35,"band_width":0.3,'
            '"depth":2,"drive":"punch","floor":0,"hold":0.8,"sustain":1.5}'
        ),
        (
            'exposure.amount:{"band_centre":0.25,"band_softness":0.35,"band_width":0.3,'
            '"depth":0.5,"drive":"sustain","floor":0.3,"hold":0.8,"sustain":1.5}'
        ),
    )
    # A stack with no binding records nothing, which is what every export before this epic was.
    assert exported_bindings([{"effect": "grain", "parameters": {"strength": 10}}]) == ()
def test_the_sendcmd_stage_sits_ahead_of_every_filter_it_drives():
    """Placement, which is a property of the picture and not of tidiness.

    `sendcmd` issues its commands while handling a frame and then passes **that** frame on, so a
    filter upstream of it does not see a new value until the frame after -- the whole clip
    delivered one tick late, on every driven parameter, with no symptom anyone could name. So the
    stage goes at the head of the chain, ahead of the labelled filter it addresses.

    The one thing that stays ahead of even this is `BRANCH_FRAME_GUARD`: it is the stage that must
    see a frame still carrying the decoder's own duration, and `sendcmd` changes no timestamp.
    """
    envelope = {"analysis_rate": 30.0, "band_count": 2,
                "bands": [[0.1] * 60, [0.2] * 60]}
    # A Geometry card beside the bound one, and it is what makes this assertion able to fail at
    # all: no Geometry *parameter* is drivable (R-29), so a bound stack composes nothing into the
    # geometry group on its own and "at the head" and "at the tail" are the same one-element
    # tuple. An unbound Punch In is what puts something there for the driver to sit ahead of.
    linear = build_effect_stages(
        [{"effect": "punch_in", "parameters": {"zoom": 1.2}},
         {"effect": "exposure", "parameters": {"amount": 0.2},
          "bindings": [{"parameter": "amount", "drive": "punch", "depth": 0.5}]}],
        width=1920, height=1080, envelope=envelope, shot_start=0.0, clip_seconds=2.0,
    )
    assert len(linear.scripts) == 1
    driver = f"sendcmd=f={linear.scripts[0].filename}"
    assert linear.geometry[0] == driver, linear.geometry
    assert len(linear.geometry) == 2 and linear.geometry[1].startswith("crop="), linear.geometry
    # `b1`, not `b0`: the label counts the effect's position in the **composed** order, and
    # the unbound Punch In takes slot zero.
    assert linear.treatment == ("eq@b1=brightness=0.2",), linear.treatment

    branched = build_effect_stages(
        [{"effect": "bloom", "parameters": {"intensity": 0.4},
          "bindings": [{"parameter": "intensity", "drive": "punch", "depth": 0.5}]}],
        width=1920, height=1080, envelope=envelope, shot_start=0.0, clip_seconds=2.0,
    )
    assert branched.branched
    # The branch frame guard stays ahead of even the driver: it is the stage that must see a
    # frame still carrying the decoder's own duration, and `sendcmd` changes no timestamp.
    assert branched.geometry == (
        BRANCH_FRAME_GUARD, f"sendcmd=f={branched.scripts[0].filename}"
    ), branched.geometry

    # And every target the compiler emits really is an `@label` in the chain composed by the same
    # call -- the one thing standing between a typo and a silently undriven export.
    for stages in (linear, branched):
        for script in stages.scripts:
            assert any(
                f"{script.target}=" in stage or f"{script.target}@" in stage
                for stage in (*stages.geometry, *stages.treatment)
            ), (script.target, stages)


def test_two_legs_of_one_transition_compose_in_separate_slot_namespaces():
    """**R-41, and the two failures it exists to prevent — one loud and one silent.**

    A transition segment reads both takes in one invocation, each leg through its own full effect
    chain, in **one** `-filter_complex`. `_branch_stage` names a branch's links `fx{slot}a` and
    `build_effect_stages` names a bound filter's instance `b{slot}`, where `slot` is the position
    in *that Shot's own* chain — and both legs start at slot 0.

    * Two **graded** Shots would emit `[fx0a]` twice in one graph. That is at least loud.
    * Two **bound** Shots would emit one `sendcmd` target — `eq@b0` — addressing the filters of
      both legs. That is silent at rc 0, and it is the class `DriveScript.target`'s docstring says
      nothing else can catch: a command aimed at a target that does not exist is accepted, ignored
      and reported nowhere, and one aimed at *two* filters drives both.

    So the namespace takes a leg prefix, and the delegated decision from 2026-08-27 is extended
    with it: **every `sendcmd` target must appear as an `@label` in the chain produced by the same
    call**, asserted here over a two-leg composition rather than one.

    `leg=""` — every existing caller — composes character for character what it always composed,
    which is the assertion at the end and is what keeps an unbound, ungraded Shot byte-identical
    in argv and valid in cache (R-20).
    """
    envelope = {"analysis_rate": 30.0, "band_count": 2, "bands": [[0.1] * 60, [0.2] * 60]}
    stack = [
        {"effect": "bloom", "parameters": {"intensity": 0.4}},
        {
            "effect": "exposure",
            "parameters": {"amount": 0.2},
            "bindings": [{"parameter": "amount", "drive": "punch", "depth": 0.5}],
        },
    ]
    legs = {
        leg: build_effect_stages(
            stack,
            width=1920,
            height=1080,
            envelope=envelope,
            shot_start=0.0,
            clip_seconds=2.0,
            leg=leg,
        )
        for leg in ("A", "B")
    }

    # The loud failure: one graph, two branches, and no label claimed twice.
    graph = " ".join(
        stage for stages in legs.values() for stage in (*stages.geometry, *stages.treatment)
    )
    assert "[fxA0a]" in graph and "[fxB0a]" in graph
    assert "[fx0a]" not in graph, "both legs claimed one branch label"

    # The silent failure: two legs, two targets, and neither reaches the other's filters.
    targets = {
        leg: [script.target for script in stages.scripts] for leg, stages in legs.items()
    }
    assert targets == {"A": ["eq@bA1"], "B": ["eq@bB1"]}
    for leg, stages in legs.items():
        for script in stages.scripts:
            # The target is an `@label` in the chain composed by **this** call...
            assert any(
                f"{script.target}=" in stage
                for stage in (*stages.geometry, *stages.treatment)
            ), (script.target, stages)
            # ...and in no stage of the other leg, which is the half a one-leg assertion cannot
            # make and the half the silent defect lives in.
            other = legs["B" if leg == "A" else "A"]
            assert not any(
                script.target in stage for stage in (*other.geometry, *other.treatment)
            ), (script.target, other)

    # And the compiled scripts are two files, not one: `_drive_script_name` carries a digest of
    # the script's own text, and the target is written into every line of it, so two legs that
    # address different filters cannot share a file and silently drive one leg twice.
    assert len({script.filename for stages in legs.values() for script in stages.scripts}) == 2

    # The identity that keeps every existing export byte-identical: no leg is the chain this
    # module has always composed, character for character.
    plain = build_effect_stages(
        stack, width=1920, height=1080, envelope=envelope, shot_start=0.0, clip_seconds=2.0
    )
    assert "[fx0a]" in " ".join((*plain.geometry, *plain.treatment))
    assert [script.target for script in plain.scripts] == ["eq@b1"]


def test_a_pair_only_entry_is_exactly_one_with_no_one_sided_form():
    """The two fields that say the same thing, held together so a thirteenth entry cannot split
    them.

    `pair_only` is what the write route refuses on; `one_sided` is what the export composes from.
    An entry that set one and forgot the other would be settable on a boundary with no Overlap
    and then compose nothing there -- rc 0, right frame count, unchanged picture, which is this
    pipeline's own recurring failure and the reason this is a test rather than a convention.

    The forms are also **distinct**, which is FX-18's *never quietly substituted* where it is
    easiest to break: one-sided there is only one picture to work with, so two names composing the
    same filter would be the catalogue substituting one for the other -- R-34's complaint about
    calling `hblur` "Blur", one level up. Dissolve and Fade through black are the pair that would
    collide if nobody said so: both are a `fade` to black, and they are separated by *when* the
    black arrives -- the end of the treatment for a dissolve, its midpoint for a dip.
    """
    for entry in TRANSITION_CATALOGUE.values():
        assert entry.pair_only == (entry.one_sided == ""), entry
        assert entry.one_sided in ("", *ONE_SIDED_FORMS), entry
    forms = [entry.one_sided for entry in TRANSITION_CATALOGUE.values() if entry.one_sided]
    assert sorted(forms) == sorted(ONE_SIDED_FORMS)
    assert len(set(forms)) == len(forms)

    # Written out rather than derived, for this module's standing reason.
    assert one_sided_transition_stages("dissolve", clip_frames=96, fps=24).treatment == (
        "fade=t=out:start_frame=84:nb_frames=12:color=black",
    )
    assert one_sided_transition_stages("fade_black", clip_frames=96, fps=24).treatment == (
        "fade=t=out:start_frame=84:nb_frames=6:color=black",
    )
    assert one_sided_transition_stages("fade_white", clip_frames=96, fps=24).treatment == (
        "fade=t=out:start_frame=84:nb_frames=6:color=white",
    )
    # And a wipe has no one-sided form at all: `None`, so the caller says so in the catalogue's
    # own sentence rather than being handed something to render.
    for transition_id in ("wipe_left", "wipe_right", "slide_up", "slide_down"):
        assert one_sided_transition_stages(transition_id, clip_frames=96, fps=24) is None


def test_a_one_sided_transition_is_bounded_by_the_clips_own_frames():
    """Story 11.4's *"bounded by the Shot's own duration and by nothing invisible"*, as arithmetic.

    The ceiling is a catalogue constant rather than a stored field (AD-19's 2026-08-29
    amendment), and the floor is the clip itself: a clip shorter than the ceiling is treated over
    its whole length rather than from a negative `start_frame`, which would be a stage ffmpeg
    accepts and a treatment that starts before the clip does.

    **The clamp is against frames and not seconds**, which is the whole reason the constant is
    declared in frames: a `start_frame` past the last frame written composes cleanly, renders at
    rc 0 and changes nothing at all.
    """
    long_clip = one_sided_transition_stages("dissolve", clip_frames=240, fps=24)
    assert long_clip.frames == ONE_SIDED_TRANSITION_FRAMES
    assert long_clip.treatment == ("fade=t=out:start_frame=228:nb_frames=12:color=black",)
    short_clip = one_sided_transition_stages("dissolve", clip_frames=5, fps=24)
    assert short_clip.frames == 5
    assert short_clip.treatment == ("fade=t=out:start_frame=0:nb_frames=5:color=black",)
    # One frame is the smallest clip the export can hold (`ASSEMBLY_TOO_SHORT_REFUSAL` refuses
    # anything shorter), and even a dip has to keep a ramp of at least one frame in it.
    single = one_sided_transition_stages("fade_black", clip_frames=1, fps=24)
    assert single.frames == 1
    assert single.treatment == ("fade=t=out:start_frame=0:nb_frames=1:color=black",)


def test_a_one_sided_blur_addresses_a_label_the_same_call_composes():
    """R-25's delegated decision, applied to the one one-sided form that is driven.

    *"Every `sendcmd` target string must appear as an `@label` in the composed chain produced by
    the same call"* -- because a command aimed at a target that is not in the graph is ignored in
    silence, and that assertion is the only thing standing between a typo and an export that is
    quietly untreated.

    **The target carries the class**, and the sentence above is why that is not decoration.
    Measured 2026-08-29 while this was written: `xo sigma 20` where `gblur@xo sigma 20` belongs
    reports *"ret:Function not implemented"* at `-v verbose` and is otherwise rc 0, silent, and
    byte-identical -- `avfilter_graph_send_command` matches a target against the filter's own name.

    The script's first line is the identity, so the ramp grows from nothing, and its last is the
    full sigma -- both written out rather than derived.

    **Each line commands the horizontal axis alone, and the init string pins the vertical one
    at 0** (R-46, 2026-08-31). `gblur` resolves `sigmaV` from `sigma` once at configuration and
    commands each axis from its own option, so a `sigma` command moves the horizontal pass only
    — measured byte-identical to a static `sigma=<commanded>:sigmaV=<init>`. Story 11.4 shipped
    this ramp commanding `sigma` alone, which made it horizontal by accident; **R-46 makes it
    horizontal on purpose**, because the paired form is `xfade=transition=hblur` and R-34 named
    the entry *"Blur wipe"* precisely so it would not call a horizontal-only effect "Blur". An
    isotropic one-sided form would make one catalogue entry render two pictures depending on
    whether an Overlap sits under it.

    **`sigmaV=0` in the init string is load-bearing, not tidy.** Measured on ffmpeg 7.0 by
    `framemd5`: `sigma=20:sigmaV=0` commanded to `sigma 0` is identical to the same chain with no
    `gblur` in it; `sigma=20` alone commanded to `sigma 0` is a 0/20 blur that never clears. The
    opening form starts blurred, so without the pin it would settle to a smeared picture at rc 0.
    """
    composed = one_sided_transition_stages("blur_wipe", clip_frames=96, fps=24)
    assert composed.treatment == (f"gblur@{ONE_SIDED_TRANSITION_LABEL}=sigma=0:sigmaV=0",)
    assert len(composed.scripts) == 1
    script = composed.scripts[0]
    assert composed.geometry == (f"sendcmd=f={script.filename}",)

    chain = ",".join((*composed.geometry, *composed.treatment))
    assert f"@{script.target.split('@')[1]}" in chain
    assert script.target in chain
    assert script.target == f"gblur@{ONE_SIDED_TRANSITION_LABEL}"
    for line in script.text.splitlines():
        assert line.split(" ")[1] == script.target, line

    lines = script.text.splitlines()
    assert len(lines) == ONE_SIDED_TRANSITION_FRAMES
    assert lines[0] == f"3.5 gblur@{ONE_SIDED_TRANSITION_LABEL} sigma 0;"
    assert lines[-1] == f"3.958333 gblur@{ONE_SIDED_TRANSITION_LABEL} sigma 20;"
    # No line touches the vertical axis: the pin is in the init string, and a command that
    # moved it would make this ramp isotropic again without changing any number above.
    assert not any("sigmaV" in line for line in lines)

    # A bare relative name, which is what `sendcmd=f=` takes with the process cwd set to the
    # file's own directory (AD-22, R-30). Every character is one that needs no escaping.
    assert set(script.filename) <= set("abcdefghijklmnopqrstuvwxyz0123456789_.-")
    # And two different ramps are two files: the digest is of the text, so an export writing a
    # short clip's ramp and a long clip's into one directory cannot have one drive the other.
    other = one_sided_transition_stages("blur_wipe", clip_frames=40, fps=24)
    assert other.scripts[0].filename != script.filename


def test_every_one_sided_form_has_exactly_one_mirror_and_no_two_share_a_picture():
    """R-45's four new forms, held to the four that already existed.

    `one_sided_in` is `one_sided`'s mirror and the two are the same fact about an entry stated in
    two directions: a type that can treat its own last frames can treat its own first ones, and a
    pair-only type can do neither (R-34). An entry that gained one and forgot the other would be
    settable and then compose nothing at that end -- rc 0, right frame count, unchanged picture,
    which is the failure `test_a_pair_only_entry_is_exactly_one_with_no_one_sided_form` exists for
    and is the reason this is a test rather than a convention.

    **And no form name appears in both tuples**, which is FX-18's *never quietly substituted*
    across the pair rather than within one: a shape that ramps away and a shape that ramps towards
    are two pictures, and one name for both would put the catalogue in the position of choosing.
    """
    for entry in TRANSITION_CATALOGUE.values():
        assert (entry.one_sided == "") == (entry.one_sided_in == ""), entry
        assert entry.pair_only == (entry.one_sided_in == ""), entry
        assert entry.one_sided_in in ("", *OPENING_FORMS), entry
    forms = [
        entry.one_sided_in for entry in TRANSITION_CATALOGUE.values() if entry.one_sided_in
    ]
    assert sorted(forms) == sorted(OPENING_FORMS)
    assert len(set(forms)) == len(forms)
    assert not set(OPENING_FORMS) & set(ONE_SIDED_FORMS)


def test_an_opening_transition_is_the_mirror_in_time_of_the_form_that_ramps_away():
    """The four opening forms, written out, beside the four they mirror.

    **The mirror is taken in time and in nothing else**, which is what keeps each of them the same
    picture R-34 measured, read backwards:

    * a dissolve spends its whole treatment arriving, at either end -- `nb_frames` is the full
      length and there is no held colour, which is what separates it from the two dips;
    * a dip **holds**: `dip_black` ramps over the first half of its window and sits in black to
      the cut, so `rise_black` sits in black from the video's first frame and ramps over the
      second half. Same halves, same order, reversed. The hold is R-34's whole distinction and it
      survives the mirror rather than being re-decided;
    * a wipe has no form in either direction, so it is `None` here as it is there, and the caller
      says so in the catalogue's own sentence instead of being handed something to render.

    Written out rather than derived from `one_sided_transition_stages`, for this module's standing
    reason: a mirror computed from the thing it mirrors cannot catch the two of them moving
    together.
    """
    assert opening_transition_stages("dissolve", clip_frames=96, fps=24).treatment == (
        "fade=t=in:start_frame=0:nb_frames=12:color=black",
    )
    assert opening_transition_stages("fade_black", clip_frames=96, fps=24).treatment == (
        "fade=t=in:start_frame=6:nb_frames=6:color=black",
    )
    assert opening_transition_stages("fade_white", clip_frames=96, fps=24).treatment == (
        "fade=t=in:start_frame=6:nb_frames=6:color=white",
    )
    for transition_id in ("wipe_left", "wipe_right", "slide_up", "slide_down"):
        assert opening_transition_stages(transition_id, clip_frames=96, fps=24) is None

    # The clamp is the tail's, against **frames**: a clip shorter than the ceiling is treated over
    # its whole length, and a `nb_frames` reaching past the last frame written is a treatment that
    # composes cleanly and changes nothing at rc 0.
    long_clip = opening_transition_stages("dissolve", clip_frames=240, fps=24)
    assert long_clip.frames == ONE_SIDED_TRANSITION_FRAMES
    assert long_clip.treatment == ("fade=t=in:start_frame=0:nb_frames=12:color=black",)
    short_clip = opening_transition_stages("dissolve", clip_frames=5, fps=24)
    assert short_clip.frames == 5
    assert short_clip.treatment == ("fade=t=in:start_frame=0:nb_frames=5:color=black",)
    single = opening_transition_stages("fade_black", clip_frames=1, fps=24)
    assert single.frames == 1
    assert single.treatment == ("fade=t=in:start_frame=0:nb_frames=1:color=black",)

    # And the two ends of one boundary are two different pictures on purpose: the opening never
    # composes the stage the closing one does, so a Director who set a Dissolve on the first Shot's
    # incoming field cannot be shown the tail's `fade=t=out` under another name.
    for transition_id in ("dissolve", "fade_black", "fade_white"):
        opening = opening_transition_stages(transition_id, clip_frames=96, fps=24)
        closing = one_sided_transition_stages(transition_id, clip_frames=96, fps=24)
        assert opening.treatment != closing.treatment, transition_id


def test_an_opening_blur_settles_from_the_sigma_it_declares_down_to_nothing():
    """`blur_ramp`'s mirror, and the one thing about it that is not symmetry for its own sake.

    The declared resting value is the **maximum** rather than zero. A `sendcmd` command timed at
    `t=0` that failed to fire would leave the picture's first frame sharp -- silently, at rc 0,
    which is the class of failure R-25 exists against -- so the filter is declared holding the
    sigma the treatment starts at and the script's first line writes the same number. The last
    line writes `0`, which is `gblur`'s measured no-op, so the ramp's own last frame is
    bit-identical to the untreated one and every frame after it is untouched. That is
    `blur_ramp`'s first frame, mirrored.

    R-25's own assertion is repeated here rather than assumed: the target string appears as an
    `@label` in the chain composed by the same call, and it carries the filter class, because
    `avfilter_graph_send_command` matches a target against the filter's own name and answers
    `ENOSYS` in silence when nothing matched.

    **The label is not the tail's**, and that is load-bearing rather than tidy: the Shot that opens
    the plan may carry an opening treatment and a one-sided one on the same chain, and two `gblur`
    instances under one name would be one `sendcmd` target driving both ramps at rc 0.
    """
    composed = opening_transition_stages("blur_wipe", clip_frames=96, fps=24)
    assert composed.treatment == (f"gblur@{OPENING_TRANSITION_LABEL}=sigma=20:sigmaV=0",)
    assert len(composed.scripts) == 1
    script = composed.scripts[0]
    assert composed.geometry == (f"sendcmd=f={script.filename}",)
    assert script.target == f"gblur@{OPENING_TRANSITION_LABEL}"
    assert script.target != f"gblur@{ONE_SIDED_TRANSITION_LABEL}"
    chain = ",".join((*composed.geometry, *composed.treatment))
    assert script.target in chain
    for line in script.text.splitlines():
        assert line.split(" ")[1] == script.target, line

    lines = script.text.splitlines()
    assert len(lines) == ONE_SIDED_TRANSITION_FRAMES
    assert lines[0] == f"0 gblur@{OPENING_TRANSITION_LABEL} sigma 20;"
    assert lines[-1] == f"0.458333 gblur@{OPENING_TRANSITION_LABEL} sigma 0;"
    # The first command writes exactly the value the filter was declared with, so a command that
    # never fires and one that fires are the same first frame.
    assert lines[0].startswith(f"0 gblur@{OPENING_TRANSITION_LABEL} sigma 20")
    assert composed.treatment[0].startswith(
        f"gblur@{OPENING_TRANSITION_LABEL}=sigma=20")
    # **The horizontal axis alone, every line, and `sigmaV` pinned at 0 by the init string**
    # (R-46). The pin is what makes the settle complete: measured by `framemd5` on ffmpeg 7.0,
    # `sigma=20:sigmaV=0` commanded to `sigma 0` is identical to the same chain with no `gblur`
    # in it, while `sigma=20` alone -- where `sigmaV` resolves to 20 at configuration --
    # commanded to `sigma 0` holds a 20-pixel vertical blur for the rest of the clip at rc 0.
    # So this pair of assertions is the whole of the difference between settling and not.
    assert composed.treatment[0].endswith(":sigmaV=0")
    for line in lines:
        assert line.count(f"gblur@{OPENING_TRANSITION_LABEL}") == 1, line
        assert " sigma " in line and "sigmaV" not in line, line

    assert set(script.filename) <= set("abcdefghijklmnopqrstuvwxyz0123456789_.-")
    # Two different ramps are two files, and two identical ones share one -- `_drive_script_name`'s
    # rule, and the export writes every clip's scripts into one directory. A clip **shorter than
    # the ceiling** is what makes an opening ramp different: unlike the tail's, this one always
    # starts at frame zero, so every clip long enough to hold the whole treatment composes the
    # same ramp and should name the same file.
    assert (
        opening_transition_stages("blur_wipe", clip_frames=240, fps=24).scripts[0].filename
        == script.filename
    )
    assert (
        opening_transition_stages("blur_wipe", clip_frames=5, fps=24).scripts[0].filename
        != script.filename
    )
    assert (
        one_sided_transition_stages("blur_wipe", clip_frames=96, fps=24).scripts[0].filename
        != script.filename
    )


def test_the_transition_catalogue_is_twelve_and_names_hblur_for_what_it_is():
    """R-34, as data rather than as prose.

    Twelve is the smallest set in which *directional* means a direction the Director picks rather
    than two of four: wipe and slide in all four, plus the four that have no direction. Every id
    resolves to a distinct `xfade` name, so FX-18's "a named type is never quietly substituted"
    holds at the catalogue as well as at the renderer.

    **`hblur` is catalogued as "Blur wipe".** ffmpeg offers 58 `xfade` transitions and not one
    isotropic blur; calling a horizontal-only effect "Blur" would be exactly the substitution
    FX-18 forbids, made by the catalogue instead of by the renderer.

    Wipes and slides are the pair-only entries — present in the list and refusing one-sided use
    with their reason, rather than silently absent from a list a Director is trying to learn.
    """
    assert len(TRANSITION_CATALOGUE) == 12
    assert len({entry.xfade for entry in TRANSITION_CATALOGUE.values()}) == 12
    assert len({entry.label for entry in TRANSITION_CATALOGUE.values()}) == 12
    assert TRANSITION_CATALOGUE["blur_wipe"].label == "Blur wipe"
    assert TRANSITION_CATALOGUE["blur_wipe"].xfade == "hblur"
    assert {
        transition_id
        for transition_id, entry in TRANSITION_CATALOGUE.items()
        if entry.pair_only
    } == {
        f"{kind}_{direction}"
        for kind in ("wipe", "slide")
        for direction in ("left", "right", "up", "down")
    }
    assert {
        transition_id
        for transition_id, entry in TRANSITION_CATALOGUE.items()
        if not entry.pair_only
    } == {"dissolve", "fade_black", "fade_white", "blur_wipe"}

    # An id no entry claims is refused by the catalogue's own sentence, which prints the whole
    # vocabulary — the shape `EFFECT_UNKNOWN_REFUSAL` already uses one catalogue over.
    with pytest.raises(EffectRefusal) as refusal:
        transition_definition("crossfade")
    assert str(refusal.value) == TRANSITION_UNKNOWN_REFUSAL.format(
        transition="crossfade", known=", ".join(sorted(TRANSITION_CATALOGUE))
    )
