"""The effects chain, asserted the way this codebase asserts every generated render input.

Standing law 10: a filter chain is a pure function of the manifest and is compared **as text**.
So almost everything here is a string equality against a stage written out by hand — not derived
from the catalogue, because a test that computed its expectation from the same table the code
reads would pass just as happily for a table that had drifted.

Three tests run the real binary, and each is here because a string cannot prove what it claims:

* the generated `.cube` round-trips through `lut3d` at ~84 dB PSNR, and the same table written
  with the loops nested the wrong way round scores ~4 dB — the mistake that reports *nothing*;
* a padded export carrying a texture leaves its letterbox bars at pure black, which is the one
  ordering constraint in this slice that is invisible in a still and wrong in a delivery;
* every stage the catalogue can emit is accepted by this project's own ffmpeg, so a typo in a
  filter option is a failed test rather than a failed export.
"""

from __future__ import annotations

import math
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
    # Every catalogued family is placed, and none is placed twice.
    assert set(PRE_SCALE_FAMILIES).isdisjoint(PRE_PAD_FAMILIES)
    assert set(PRE_SCALE_FAMILIES) | set(PRE_PAD_FAMILIES) == set(FAMILY_ORDER)
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
    runs before `scale` and has no idea what shape the take is."""
    built = stages([effect("dutch_tilt", angle=10)])
    radians = math.radians(10)
    cosine = f"{abs(math.cos(radians)):.6f}".rstrip("0").rstrip(".")
    sine = f"{abs(math.sin(radians)):.6f}".rstrip("0").rstrip(".")
    inscribed = f"max((iw*{cosine}+ih*{sine})/iw\\,(iw*{sine}+ih*{cosine})/ih)"
    assert built.geometry == (
        f"rotate=a={f'{radians:.6f}'.rstrip('0').rstrip('.')}:ow=iw:oh=ih",
        f"crop=w=iw/{inscribed}:h=ih/{inscribed}:x=(iw-ow)/2:y=(ih-oh)/2",
    )
    # At zero the pair is a no-op the chain can carry harmlessly: no rotation, factor 1.
    unit = r"max((iw*1+ih*0)/iw\,(iw*0+ih*1)/ih)"
    assert stages([effect("dutch_tilt")]).geometry == (
        "rotate=a=0:ow=iw:oh=ih",
        f"crop=w=iw/{unit}:h=ih/{unit}:x=(iw-ow)/2:y=(ih-oh)/2",
    )


# ------------------------------------------------------------------------------------------
# Defaults, and the refusals. Every one names its offender.
# ------------------------------------------------------------------------------------------


def test_an_omitted_parameter_takes_the_catalogues_default():
    """And the default is written out here rather than read from the catalogue, so a default
    that drifted would fail this test rather than redefine it."""
    assert stages([effect("grain")]).treatment == ("noise=alls=0:allf=t+u:all_seed=0",)
    assert stages([effect("contrast")]).treatment == ("eq=contrast=1",)
    assert stages([effect("punch_in")]).geometry == (
        "crop=w=iw/1:h=ih/1:x=(iw-ow)/2:y=(ih-oh)/2",
    )
    assert stages([effect("mirror")]).geometry == ("hflip",)
    assert stages([effect("posterize")]).treatment == ("lutyuv=y=trunc(val/1)*1",)
    # A spec carrying no `parameters` key at all is the same thing as one carrying an empty map.
    assert stages([{"effect": "grain"}]) == stages([effect("grain")])


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
    # The bounds themselves are inside.
    assert stages([effect("grain", strength=60)]).treatment[0].startswith("noise=alls=60")
    assert stages([effect("punch_in", zoom=1)]).geometry


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


def test_two_files_whose_names_collide_get_stable_distinct_ids(tmp_path: Path):
    """The id is lossy by design, so a collision is possible. It is resolved in the folder's
    sorted order, which makes the ids the same on every run and on every machine."""
    directory = lut_directory(tmp_path)
    directory.mkdir(parents=True)
    for name in ("Warm Shift.cube", "warm_shift.cube", "warm-shift.cube"):
        (directory / name).write_text(cube_text(2, identity_transform), encoding="utf-8")
    ids = [entry.lut_id for entry in discover_luts(tmp_path)]
    assert ids == ["warm-shift", "warm-shift-2", "warm-shift-3"]
    assert ids == [entry.lut_id for entry in discover_luts(tmp_path)]
    assert lut_id_for_name("Kodak 2383 (D65)!") == "kodak-2383-d65"
    assert lut_id_for_name("...") == "lut"


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


def test_the_generated_looks_stay_inside_the_domain_and_are_reproducible():
    """Clamped, so a look cannot write a value ffmpeg would clip into a highlight nobody asked
    for; and byte-identical between runs, because a generated render input is a pure function."""
    for lut_id, title, transform in DEFAULT_LUTS:
        text = cube_text(5, transform, title=title)
        assert text == cube_text(5, transform, title=title), lut_id
        for line in text.splitlines()[4:]:
            assert all(0.0 <= float(value) <= 1.0 for value in line.split())


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
    """Belt and braces on the one place a client string could have become a path. The stack
    below names an id that exists; the entry it resolves to is the one the *server* built, and
    the composed stage carries that path and no part of the client's string."""
    directory = lut_directory(tmp_path)
    directory.mkdir(parents=True)
    (directory / "house.cube").write_text(cube_text(2, identity_transform), encoding="utf-8")
    luts = discover_luts(tmp_path)
    assert luts == (LutEntry(lut_id="house", name="house", path=directory / "house.cube"),)
    built = stages([effect("lut_look", lut="house")], luts=luts)
    assert (directory / "house.cube").as_posix().replace(":", r"\:") in built.treatment[0]


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
