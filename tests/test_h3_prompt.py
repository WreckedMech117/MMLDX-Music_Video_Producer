"""The H3 prompt checker, against the guide's own worked example and against the
mistakes its checklist implies people actually make.

The good-case prompt below is paraphrased from MiniMax's Case 1 (T2VA) worked
example rather than copied: the *structure* is what is being asked about, and the
guide is a third-party document this repository deliberately does not reproduce.
Every structural feature it exercises — the three fields in order, `[Shot 1]`
unstamped, a later shot with an increasing cut time, a language-tagged `<d>` block,
a speaker id — is present.
"""

from __future__ import annotations

import pytest

from music_video_producer.h3_prompt import (
    CORE_FIELDS,
    NOT_APPLICABLE,
    check,
    check_dialogue,
    check_orphan_cuts,
    check_retention,
    check_shots,
)

GOOD_T2VA = """integrated_multimodal_description: [Shot 1] Live-action, cinematic, a medium-wide shot frames a baker opening the shutters of a small street bakery before sunrise. The camera pushes in slowly as the middle-aged baker with a calm, slightly raspy voice (S1) places a fresh loaf on the counter and says: <d>[English] First batch of the morning.</d> [Shot 2] At 00:05.000, the camera cuts to a close-up of steam rising from the sliced bread while the baker's final words carry over from the previous shot.
overall_soundscape: Wooden shutters scrape open over a quiet street as trays clink softly inside. The doorbell rings once, followed by light footsteps.
non_diegetic_music: A soft acoustic-guitar pattern at a moderate tempo, joined by sparse upright-bass notes and a gentle fade at the end."""


def test_the_guides_own_worked_structure_is_well_formed() -> None:
    """If the checker rejects the format the guide demonstrates, the checker is wrong."""
    result = check(GOOD_T2VA, duration=8.0)
    assert result.well_formed, [problem.message for problem in result.fatal]
    assert list(result.fields) == list(CORE_FIELDS)
    assert result.instruction == ""


def test_well_formed_is_not_a_claim_that_the_prompt_is_good() -> None:
    """A sentence in the right wrapper passes every mechanical check.

    This is the module's own limit, asserted so nobody later reads a clean result as
    approval. The semantic rules — every cut introducing new information, only
    vocalizing characters carrying ids, amplitude only where meaningful — live in the
    specialist's system prompt because nothing here can decide them.
    """
    thin = (
        f"{CORE_FIELDS[0]}: [Shot 1] A grey wolf paces through trees.\n"
        f"{CORE_FIELDS[1]}: Wind moves through branches.\n"
        f"{CORE_FIELDS[2]}: {NOT_APPLICABLE}"
    )
    assert check(thin).well_formed


def test_a_bare_sentence_is_refused_because_it_has_no_fields() -> None:
    """What the application submitted before this existed."""
    result = check("A grey wolf pacing through trees under amber light; 35mm lens.")
    assert not result.well_formed
    assert any("core fields" in problem.message for problem in result.fatal)


def test_the_fields_must_be_in_the_guides_order() -> None:
    out_of_order = (
        f"{CORE_FIELDS[0]}: [Shot 1] A street at dawn.\n"
        f"{CORE_FIELDS[2]}: {NOT_APPLICABLE}\n"
        f"{CORE_FIELDS[1]}: Traffic hums two streets away."
    )
    result = check(out_of_order)
    assert not result.well_formed
    assert any("out of order" in problem.message for problem in result.fatal)


def test_a_value_may_sit_on_the_line_beneath_its_label() -> None:
    """The guide's full-reference example formats them this way."""
    beneath = (
        f"{CORE_FIELDS[0]}:\n[Shot 1] A street at dawn.\n"
        f"{CORE_FIELDS[1]}:\nTraffic hums two streets away.\n"
        f"{CORE_FIELDS[2]}:\n{NOT_APPLICABLE}"
    )
    result = check(beneath)
    assert result.well_formed, [problem.message for problem in result.fatal]
    assert result.fields[CORE_FIELDS[0]].startswith("[Shot 1]")


def test_shot_one_may_not_carry_a_timestamp() -> None:
    problems = check_shots("[Shot 1] At 00:00.000, a street at dawn.")
    assert any("must not carry a timestamp" in problem.message for problem in problems)


def test_cut_times_must_strictly_increase() -> None:
    problems = check_shots(
        "[Shot 1] A street. [Shot 2] At 00:05.000, a door. [Shot 3] At 00:03.000, a window."
    )
    assert any("does not advance" in problem.message for problem in problems)


def test_two_shots_may_not_share_a_cut_time() -> None:
    """The guide says cut times *strictly* increase, so equal is as wrong as backwards.

    Written because a mutation weakening the comparison from `<=` to `<` passed every
    other test here: the decreasing case above was covered and the equal case was not,
    so "strictly" was asserted nowhere. A cut that does not advance is a shot boundary
    with no time of its own, which is exactly the ambiguity the rule exists to prevent.
    """
    problems = check_shots(
        "[Shot 1] A street. [Shot 2] At 00:05.000, a door. [Shot 3] At 00:05.000, a window."
    )
    assert any("does not advance" in problem.message for problem in problems)


def test_a_cut_beyond_the_shots_own_length_is_reported() -> None:
    problems = check_shots("[Shot 1] A street. [Shot 2] At 00:09.000, a door.",
                           duration=3.75)
    assert any("beyond" in problem.message for problem in problems)


def test_shots_must_be_numbered_in_order() -> None:
    problems = check_shots("[Shot 1] A street. [Shot 3] At 00:05.000, a door.")
    assert any("numbered in order" in problem.message for problem in problems)


def test_every_shot_after_the_first_needs_a_cut_time() -> None:
    problems = check_shots("[Shot 1] A street. [Shot 2] a door.")
    assert any("no cut time" in problem.message for problem in problems)


def test_dialogue_tags_must_balance() -> None:
    problems = check_dialogue("[Shot 1] She says <d>[English] Hello.")
    assert any("unbalanced" in problem.message for problem in problems)


def test_a_dialogue_block_needs_a_language_tag() -> None:
    problems = check_dialogue("[Shot 1] She says <d>Hello.</d>")
    assert any("no language tag" in problem.message for problem in problems)


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("overall_soundscape", "One. Two. Three. Four. Five.", True),
        ("overall_soundscape", "Just the one.", False),
        ("non_diegetic_music", "One. Two. Three. Four.", True),
        ("non_diegetic_music", "Strings swell and fade.", False),
    ],
)
def test_the_sound_fields_carry_the_guides_sentence_bounds(
    name: str, value: str, expected: bool
) -> None:
    """Over-long is advisory rather than fatal: it is a style note, not a break."""
    prompt = (
        f"{CORE_FIELDS[0]}: [Shot 1] A street at dawn.\n"
        f"{CORE_FIELDS[1]}: {value if name == CORE_FIELDS[1] else 'Traffic hums.'}\n"
        f"{CORE_FIELDS[2]}: {value if name == CORE_FIELDS[2] else NOT_APPLICABLE}"
    )
    result = check(prompt)
    flagged = any(problem.field == name for problem in result.problems)
    assert flagged is expected
    assert result.well_formed


def test_not_applicable_is_exempt_from_the_sentence_bounds() -> None:
    prompt = (
        f"{CORE_FIELDS[0]}: [Shot 1] A street at dawn.\n"
        f"{CORE_FIELDS[1]}: Traffic hums two streets away.\n"
        f"{CORE_FIELDS[2]}: {NOT_APPLICABLE}"
    )
    assert not any(problem.field == CORE_FIELDS[2] for problem in check(prompt).problems)


def test_a_speaker_id_in_retention_analysis_is_refused() -> None:
    """The guide gives this its own checklist line, which suggests it is a real habit."""
    problems = check_retention(
        "retention_analysis:\n<Subject 1> (S1) fully_preserved - face and wardrobe kept.\n"
    )
    assert problems and "belong in the description" in problems[0].message


def test_retention_analysis_without_a_speaker_id_is_accepted() -> None:
    assert not check_retention(
        "retention_analysis:\n<Subject 1> fully_preserved - face and wardrobe kept.\n"
    )


def test_a_keyframe_mode_requires_its_instruction_line() -> None:
    result = check(GOOD_T2VA, expect_instruction=True)
    assert not result.well_formed
    assert any(problem.field == "instruction" for problem in result.fatal)


def test_an_instruction_line_on_a_text_only_mode_is_advisory_not_fatal() -> None:
    """A mode confusion worth surfacing, but the prompt itself is still usable."""
    with_instruction = (
        "For the target video, at 0.00 seconds into the target video, <Picture 1> "
        "(from [Shot 1]) is fully referenced.\n\n" + GOOD_T2VA
    )
    result = check(with_instruction)
    assert result.well_formed
    assert any(problem.field == "instruction" and not problem.fatal
               for problem in result.problems)


def test_a_repeated_field_is_reported_rather_than_silently_overwritten() -> None:
    repeated = (
        f"{CORE_FIELDS[0]}: [Shot 1] A street at dawn.\n"
        f"{CORE_FIELDS[1]}: Traffic hums.\n"
        f"{CORE_FIELDS[2]}: {NOT_APPLICABLE}\n"
        f"{CORE_FIELDS[1]}: A different soundscape entirely."
    )
    result = check(repeated)
    assert not result.well_formed
    assert any("more than once" in problem.message for problem in result.fatal)
    assert result.fields[CORE_FIELDS[1]] == "Traffic hums."


def test_fields_run_together_on_one_line_are_diagnosed_as_that() -> None:
    """The first live run produced exactly this, and "missing" was the wrong word.

    A local model asked for three fields each on its own line put all three on one.
    They were present; the parser could not read them. Reporting that as *missing*
    sends a reader hunting for something already in front of them, and the fix is a
    line break rather than a rewrite — so the message has to say which it is.

    The misleading half is also suppressed: a field diagnosed as inline is not then
    also reported as missing by the sound-field check, because two contradictory
    sentences about one field is worse than one accurate one.
    """
    one_line = (
        f"{CORE_FIELDS[0]}: [Shot 1] A wolf walks. "
        f"{CORE_FIELDS[1]}: Leaves crunch underfoot. "
        f"{CORE_FIELDS[2]}: Cello in a minor key, swelling then receding."
    )
    result = check(one_line, duration=3.75)
    assert not result.well_formed
    messages = [problem.message for problem in result.problems]
    assert all("appears mid-line" in message for message in messages), messages
    assert not any("is missing" in message for message in messages), messages


def test_a_cut_time_with_no_shot_marker_is_prose_not_a_cut() -> None:
    """Found by measuring the live model, not by reading the guide.

    Asked for a short clip, it wrote `[Shot 1] ... At 00:02.500 A grey wolf steps ...
    At 00:03.750 Close on her face` — and every other check here passed it. It reads as a
    three-shot prompt and is not one: H3 takes shot boundaries from `[Shot N]`, so those
    times are prose inside one continuous shot.

    Worth checking precisely because it is invisible to the eye that wrote it. The intent is
    legible to a human reader, which is exactly what makes it easy to ship.
    """
    problems = check_orphan_cuts(
        "[Shot 1] She stands still. At 00:02.500 A wolf steps in. At 00:03.750 Close on her."
    )
    assert len(problems) == 2
    assert all("no [Shot N] in front of it" in problem.message for problem in problems)


def test_a_cut_time_belonging_to_a_shot_marker_is_not_flagged() -> None:
    """The guard must not fire on the correct form, or it would reject every real prompt."""
    assert not check_orphan_cuts("[Shot 1] She stands. [Shot 2] At 00:02.500 A wolf steps in.")


def test_defer_audio_fields_replaces_only_the_two_audio_fields():
    """The song-audio deferral, measured before it was written (2026-08-19): the
    specialist's own audio fields drowned the referenced track (envelope correlation
    0.36/0.27 with fields vs 0.84 bare, same shot, same seed), so a song-audio shot's
    stored expansion defers both fields to the reference. The description — the picture —
    is untouched byte for byte, the result stays checker-clean, and the rewrite is
    idempotent."""
    from music_video_producer.h3_prompt import (
        SONG_AUDIO_MUSIC,
        SONG_AUDIO_SOUNDSCAPE,
        defer_audio_fields,
    )

    sample = (
        "integrated_multimodal_description: [Shot 1] She sings at the mic, camera "
        "pushing in slowly.\n\n"
        "overall_soundscape: Warehouse echo hums; mic stand clicks softly.\n\n"
        "non_diegetic_music: driving electric guitars swell beneath her vocal line."
    )
    out = defer_audio_fields(sample)
    assert out.startswith(
        "integrated_multimodal_description: [Shot 1] She sings at the mic, camera "
        "pushing in slowly."
    )
    assert SONG_AUDIO_SOUNDSCAPE in out
    assert SONG_AUDIO_MUSIC in out
    assert "Warehouse echo" not in out
    assert "electric guitars swell" not in out
    assert check(out, duration=4.0).problems == []
    assert defer_audio_fields(out) == out
    # A malformed document is the checker's problem, not this normalizer's.
    assert defer_audio_fields("integrated_multimodal_description: x") == (
        "integrated_multimodal_description: x"
    )
    assert defer_audio_fields("free text, no fields") == "free text, no fields"
