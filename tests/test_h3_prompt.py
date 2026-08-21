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
    REFERENCE_FIELD,
    check,
    check_dialogue,
    check_orphan_cuts,
    check_reference_bounds,
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
    # "At **or beyond**", and the boundary was asserted nowhere until 2026-08-20 — a mutation
    # weakening `>=` to `>` passed this whole file. A cut at exactly the clip's length is one
    # frame past its last, so it names no moment the render contains. Same bound the stray-cut
    # scan uses; they are the same rule seen from two sides.
    assert any("at or beyond" in problem.message for problem in check_shots(
        "[Shot 1] A street. [Shot 2] At 00:04.000, a door.", duration=4.0))
    assert check_shots("[Shot 1] A street. [Shot 2] At 00:03.999, a door.",
                       duration=4.0) == []


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


def test_normalize_audio_fields_rewrites_both_to_the_reuse_declaration() -> None:
    """The song-audio normalization, measured before it was written (2026-08-19): the
    specialist's freely-written fields drowned the referenced track (0.36/0.27) and
    untagged deferral prose recovered unreliably (0.36-0.73). The stored shape is the
    guide's own §2.6 form — `non_diegetic_music` cites the track by tag as the
    directly-reused score, `overall_soundscape` goes N/A — and the description is
    untouched byte for byte."""
    from music_video_producer.h3_prompt import normalize_audio_fields

    sample = (
        "integrated_multimodal_description: [Shot 1] She sings at the mic, camera "
        "pushing in slowly.\n\n"
        "overall_soundscape: Warehouse echo hums; mic stand clicks softly.\n\n"
        "non_diegetic_music: driving electric guitars swell beneath her vocal line."
    )
    out = normalize_audio_fields(sample, audio_tag=1)
    assert out == (
        "integrated_multimodal_description: [Shot 1] She sings at the mic, camera "
        "pushing in slowly.\n\n"
        "overall_soundscape: N/A\n\n"
        "non_diegetic_music: <Audio 1> is directly reused as the complete "
        "audience-only score."
    )
    assert normalize_audio_fields(out, audio_tag=1) == out  # idempotent
    # The normalized shape is a fully legal prompt under the default checker.
    assert check(out, duration=4.0).problems == []
    # The tag follows the render's numbering, whatever it is.
    assert "<Audio 3> is directly reused" in normalize_audio_fields(sample, audio_tag=3)
    # A prompt missing both fields passes through untouched; the checker owns malformed
    # documents. `require_sound_fields=False` accepts that shape while still
    # bounds-checking any field that IS present.
    headless = (
        "integrated_multimodal_description: [Shot 1] She sings at the mic, camera "
        "pushing in slowly."
    )
    assert normalize_audio_fields(headless) == headless
    assert any("missing" in p.message for p in check(headless, duration=4.0).problems)
    assert check(headless, duration=4.0, require_sound_fields=False).problems == []
    overlong = headless + "\n\noverall_soundscape: One. Two. Three. Four. Five. Six."
    assert any(
        "sentence" in p.message
        for p in check(overlong, duration=4.0, require_sound_fields=False).problems
    )
    assert normalize_audio_fields("free text, no fields") == "free text, no fields"


def test_forbid_dialogue_flags_any_d_block_on_a_song_audio_prompt() -> None:
    """The prohibition that failed as a rule (2026-08-19): the model invented fully
    well-formed lyrics — words in no lyric sheet — inside <d>[English] on a not-singing
    shot, and every other check passed them. On a shot conditioned on the master song any
    <d> block is a second vocal source, so its presence is the mechanical defect."""
    invented = (
        "integrated_multimodal_description: [Shot 1] She stands at the mic. "
        '<d>[English] "Words from nowhere"</d>\n\n'
        "overall_soundscape: N/A\n\n"
        "non_diegetic_music: <Audio 1> is directly reused as the complete "
        "audience-only score."
    )
    assert check(invented, duration=4.0).problems == []  # well-formed by every old rule
    flagged = check(invented, duration=4.0, forbid_dialogue=True).problems
    assert any("no <d> dialogue block" in p.message for p in flagged)
    clean = invented.replace('<d>[English] "Words from nowhere"</d>', "She sings.")
    assert check(clean, duration=4.0, forbid_dialogue=True).problems == []


# --------------------------------------------------------------------------------------------
# Reference-slot bounds. H3's media slots are anonymous: `<Picture N>` *is* the Nth picture the
# payload wires, and nothing else names it, so a tag past the last attached picture renders
# plausibly and wrongly instead of failing.
# --------------------------------------------------------------------------------------------


def reference_prompt_citing(*tags: str) -> str:
    """A well-formed prompt whose description cites exactly `tags`."""
    return (
        f"{CORE_FIELDS[0]}: [Shot 1] {' and '.join(tags)} stand in the warehouse.\n"
        f"{CORE_FIELDS[1]}: Warehouse air hums.\n"
        f"{CORE_FIELDS[2]}: {NOT_APPLICABLE}"
    )


def test_citing_a_picture_past_the_last_attached_one_is_fatal_and_names_the_slot() -> None:
    """The invariant, in the direction that has to block: three cited, two attached."""
    problems = check_reference_bounds(
        reference_prompt_citing("<Picture 1>", "<Picture 2>", "<Picture 3>"),
        slots={"picture": 2},
    )
    assert [problem.fatal for problem in problems] == [True]
    assert problems[0].field == REFERENCE_FIELD
    assert "<Picture 3> is cited" in problems[0].message
    assert "only 2 pictures are attached" in problems[0].message
    assert "<Picture 2> is the highest tag" in problems[0].message


def test_a_prompt_citing_every_attached_picture_has_no_problem_at_all() -> None:
    """The guard must not fire on the correct form, or it refuses every real shot."""
    assert check_reference_bounds(
        reference_prompt_citing("<Picture 1>", "<Picture 2>"), slots={"picture": 2}
    ) == []


def test_an_attached_but_unmentioned_picture_warns_and_never_blocks() -> None:
    """The weak direction, deliberately advisory — `batch.readiness_report`'s split between
    emptiness (blocks) and sameness (warns). A style or lighting reference the Director
    attached on purpose may have nothing useful to say about it in prose, and a gate that
    refuses that gets switched off along with the true refusals."""
    problems = check_reference_bounds(
        reference_prompt_citing("<Picture 1>"), slots={"picture": 3}
    )
    assert [problem.fatal for problem in problems] == [False, False]
    assert "<Picture 2> is attached" in problems[0].message
    assert "<Picture 3> is attached" in problems[1].message
    assert all("does not block" in problem.message for problem in problems)
    # And it stays submittable: `well_formed` is "nothing fatal", so a shot whose only
    # reference problem is an unmentioned picture still passes the gate.
    parsed = check(
        reference_prompt_citing("<Picture 1>"), duration=4.0, reference_slots={"picture": 3}
    )
    assert parsed.well_formed
    assert len(parsed.problems) == 2


def test_a_text_only_prompt_with_no_references_is_not_refused() -> None:
    """Zero attached, zero cited. A text-to-video shot must pass untouched, in both
    directions: no under-citation warning to invent, no over-citation to report."""
    assert check_reference_bounds(GOOD_T2VA, slots={"picture": 0, "video": 0, "audio": 0}) == []
    assert check(GOOD_T2VA, duration=8.0,
                 reference_slots={"picture": 0, "video": 0, "audio": 0}).problems == []


def test_a_reference_tag_on_a_shot_with_nothing_attached_says_so_in_those_words() -> None:
    """The zero case needs its own sentence: "only 0 pictures are attached" is not English,
    and the fix is different — attach something, rather than renumber."""
    problems = check_reference_bounds(
        reference_prompt_citing("<Picture 1>"), slots={"picture": 0}
    )
    assert [problem.fatal for problem in problems] == [True]
    assert "no pictures attached at all" in problems[0].message


def test_the_three_kinds_are_numbered_independently_of_each_other() -> None:
    """The edge the real numbering scheme creates. The conditioner wires pictures, videos and
    audios into three separate per-kind slot lists — `app.reference_map_tag_lines` and the
    submit route each keep three counters — so `<Video 1>` on a shot with one picture and one
    video is correct, and a checker sharing one counter across the kinds would refuse it."""
    mixed = reference_prompt_citing("<Picture 1>", "<Video 1>", "<Audio 1>")
    assert check_reference_bounds(mixed, slots={"picture": 1, "video": 1, "audio": 1}) == []
    # One counter shared across kinds would have read this as three references and passed
    # `<Picture 2>`; three counters refuse it, which is the payload's own truth.
    over = check_reference_bounds(
        reference_prompt_citing("<Picture 2>", "<Video 1>", "<Audio 1>"),
        slots={"picture": 1, "video": 1, "audio": 1},
    )
    assert [problem.fatal for problem in over] == [True, False]
    assert "<Picture 2> is cited" in over[0].message
    assert "<Picture 1> is attached" in over[1].message


def test_a_kind_the_caller_omits_is_skipped_in_both_directions() -> None:
    """The contract's load-bearing clause. A caller that cannot count a kind leaves it out,
    and gets no answer about it rather than a guessed one — an unreliable gate that refuses a
    valid shot is worse than no gate."""
    prompt = reference_prompt_citing("<Picture 9>", "<Video 4>", "<Audio 7>")
    assert check_reference_bounds(prompt, slots={}) == []
    only_video = check_reference_bounds(prompt, slots={"video": 1})
    # `<Video 4>` over-cited and `<Video 1>` unmentioned; the omitted kinds say nothing at all,
    # even though `<Picture 9>` and `<Audio 7>` are sitting right there in the same prompt.
    assert [problem.fatal for problem in only_video] == [True, False]
    assert "<Video 4> is cited" in only_video[0].message
    assert not any("Picture" in problem.message or "Audio" in problem.message
                   for problem in only_video)
    # `None` is the same skip one layer up, and is the default: every caller that passes
    # nothing keeps the exact result it had before this check existed.
    assert check(prompt, duration=4.0).problems == []


def test_slot_zero_is_reported_as_a_numbering_error_rather_than_as_over_citation() -> None:
    """`<Picture 0>` is the same defect from the other end, and "only 2 pictures are
    attached" would be an unhelpful thing to say about it."""
    problems = check_reference_bounds(
        reference_prompt_citing("<Picture 0>", "<Picture 1>", "<Picture 2>"),
        slots={"picture": 2},
    )
    assert [problem.fatal for problem in problems] == [True]
    assert "numbered from 1" in problems[0].message


def test_the_guides_own_audio_reuse_declaration_bounds_against_the_song_slot() -> None:
    """The tag `normalize_audio_fields` writes lives in `non_diegetic_music`, not in the
    description, so the check reads the whole prompt rather than one field. A song-audio shot
    wires exactly one audio — the master song — and the declaration cites it."""
    song_audio = (
        "integrated_multimodal_description: [Shot 1] <Picture 1> sings to camera.\n\n"
        "overall_soundscape: N/A\n\n"
        "non_diegetic_music: <Audio 1> is directly reused as the complete "
        "audience-only score."
    )
    assert check(song_audio, duration=4.0,
                 reference_slots={"picture": 1, "video": 0, "audio": 1}).problems == []
    # The same prompt on a shot that does NOT ride the song cites an audio slot nothing fills.
    detached = check(song_audio, duration=4.0,
                     reference_slots={"picture": 1, "video": 0, "audio": 0})
    assert not detached.well_formed
    assert any("<Audio 1> is cited" in problem.message for problem in detached.fatal)


def test_a_subject_tag_is_never_bounds_checked() -> None:
    """`<Subject N>` is defined by the prompt itself — `<Subject 1> is the character from
    <Picture 1>` — and is wired into no payload slot, so it has no count to exceed. Bounding
    it would refuse the guide's own retention form on every single-picture shot."""
    with_subject = (
        f"{CORE_FIELDS[0]}: [Shot 1] <Picture 1> turns to camera.\n"
        f"{CORE_FIELDS[1]}: Room tone.\n"
        f"{CORE_FIELDS[2]}: {NOT_APPLICABLE}\n"
        "retention_analysis:\n<Subject 4> fully_preserved - face and wardrobe kept."
    )
    assert check_reference_bounds(with_subject, slots={"picture": 1}) == []


def test_a_lowercased_tag_is_still_bounded_because_the_number_is_the_defect() -> None:
    """A model that wrote `<picture 3>` onto a two-picture shot made exactly this mistake, and
    matching only the guide's capitalisation would ship it."""
    problems = check_reference_bounds(
        reference_prompt_citing("<picture 1>", "<picture 3>"), slots={"picture": 2}
    )
    assert [problem.fatal for problem in problems] == [True, False]
    assert "<Picture 3> is cited" in problems[0].message


# --------------------------------------------------------------------------------------------
# Casing. Every cut-time check here matched a literal capital `At` until 2026-08-20, so
# `at 00:02.500` — the casing the local models actually produce — walked through all of them.
# The audit that followed found the same latent assumption in `<d>` and in the speaker id.
# --------------------------------------------------------------------------------------------


#: The real thing, byte for byte: Qwythos' accepted, "well-formed" answer for S1 in the
#: 2026-08-20 live expansion bake-off
#: (`test-artifacts/2026-08-20-lmstudio-live/run6-expansions-qwythos.json`, second attempt,
#: which is also its stored `final_text`). Five lower-case prose timestamps under one
#: `[Shot 1]` — three in the description, one in each sound field — and `at 00:06.250` is past
#: the end of the 5.665s clip. The checker passed it, and it would have been submitted.
#: Quoted rather than paraphrased on purpose: a synthesised approximation would have been
#: written by the same understanding that missed the defect.
QWYTHOS_S1_LOWER_CASE_CUTS = (
    "instruction line for keyframe alignment: <Picture 1> aligns to the start of this clip, "
    "<Picture 2> to its end, so the renderer can blend them into a seamless push‑in from "
    "tight mic stand framing to wide canopy bed composition; one blank line below that\n"
    "\n"
    "integrated_multimodal_description: [Shot 1] HarderFaster, a woman in her late twenties "
    "with waist-length copper hair, a black leather corset and a silver studded belt, stands "
    "at the mic stand downstage bathed in a cone of moonlight; she sings straight into the "
    "lens over the window, breath visible in the cool air as <Audio 1> drives her lip "
    "movements. The single bare bulb above casts a hard rim light across her face while dust "
    "hangs motionless from the high warehouse windows; at 00:02.500 she steps back and pivots "
    "toward the upstage space as the camera pushes in slowly, her corset’s silver studded "
    "belt catching the electric-blue highlights. The mic stand creaks under her weight with "
    "each movement of her torso, a subtle physical sound that anchors the moment without "
    "competing with the score; at 00:04.750 she has crossed into the moonlit expanse and "
    "finally turns fully away from the lens, revealing the canopy bed upstage where black "
    "drapery pools beneath her silhouette. The room rings empty around her—no other "
    "performer is in frame yet—but the wide framing now swallows more of the "
    "warehouse’s deep shadows, letting the high windows bleed their light across the "
    "expanse; at 00:06.250 she lands on that final gesture, hands free and body angled toward "
    "the bed as if responding to the space itself rather than the camera.\n"
    "\n"
    "overall_soundscape: The warehouse hums with a near-monochrome stillness broken only by "
    "dust motes drifting through moonlight from the high windows; a single bare bulb hangs "
    "over the mic stand, its filament buzzing faintly while she stands beneath it. Her breath "
    "escapes in cool puffs as she sings, and every pivot sends a soft creak of leather against "
    "metal that registers sharply against the quiet room; at 00:04.750 her turn opens up the "
    "moonlit expanse, revealing more of the deep shadows where light from above spills onto "
    "the black drapery pooling beneath the canopy bed.\n"
    "\n"
    "non_diegetic_music: Hard rock with driving electric guitars and raw female vocals, the "
    "score pushes the camera’s motion forward while the bass pulses under her steps; at "
    "00:04.750 she turns into that momentum as if the beat is pulling her toward the upstage "
    "space, the whole track swelling with 80s metal energy."
)


def test_the_captured_lower_case_cut_prompt_is_the_regression_guard() -> None:
    """The instance this fix exists for, quoted from the run that produced it.

    It is the exact document the expansion loop accepted and would have submitted. Every
    other mechanical rule passed it: one shot marker, correctly unstamped, three fields in
    order, no `<d>` block, one audio cited against one attached. The only thing wrong with it
    is five timestamps that H3 will read as prose, and the shift key hid all five.
    """
    result = check(
        QWYTHOS_S1_LOWER_CASE_CUTS,
        duration=5.665,
        expect_instruction=True,
        forbid_dialogue=True,
        reference_slots={"picture": 2, "video": 0, "audio": 1},
    )
    assert not result.well_formed
    messages = [problem.message for problem in result.fatal]
    # All three description timestamps, by their own text and in their own casing.
    for stamp in ("at 00:02.500", "at 00:04.750", "at 00:06.250"):
        assert any(f"'{stamp}' has no [Shot N] in front of it" in message
                   for message in messages), (stamp, messages)
    # One in each sound field, which the old scan could not see at all.
    for name in (CORE_FIELDS[1], CORE_FIELDS[2]):
        assert any(problem.field == name and "is a cut time written into" in problem.message
                   for problem in result.fatal), (name, messages)
    # And the one past the end of the clip, as its own distinct sentence.
    assert any("'at 00:06.250' is at 6.250s, at or beyond the 5.67s" in message
               for message in messages), messages
    # The instruction line is deliberately unscanned, so nothing is reported about it.
    assert not any(problem.field == "instruction" for problem in result.problems)


def test_the_keyframe_instruction_line_may_carry_a_timecode_of_its_own() -> None:
    """Measured, not assumed. Qwen3-35B's S1 in the same bake-off opened with

        Picture 1 aligns at 00:00.000, Picture 2 aligns at 00:05.665.

    on a 5.665s clip — a correct alignment claim whose second time is the clip's own last
    frame. Scanning the instruction line for stray cuts, or bounds-checking the times in it,
    would report two fatal problems about a line doing exactly what its mode asked for. A gate
    that refuses the correct form is worse than no gate, because it gets switched off and takes
    the true refusals with it. The prompt was submittable before the casing fix and stays
    submittable after it, with no cut-time problem raised about it anywhere.

    Its two *advisory* problems are a different defect and were reported before this change:
    the pictures are named "Picture 1" without angle brackets, so `check_reference_bounds`
    reports both attached slots as never mentioned. That is left advisory on purpose — see
    `test_an_attached_but_unmentioned_picture_warns_and_never_blocks`.
    """
    qwen_s1 = (
        "Picture 1 aligns at 00:00.000, Picture 2 aligns at 00:05.665.\n"
        f"{CORE_FIELDS[0]}: [Shot 1] The camera starts tight on HarderFaster singing into a "
        "standing microphone; her mouth moves precisely with the words of <Audio 1>. The "
        "camera pushes in slowly, ending wide as she turns slightly away from the lens.\n"
        f"{CORE_FIELDS[1]}: The empty warehouse holds its own resonance; a bare bulb hangs "
        "over the mic stand.\n"
        f"{CORE_FIELDS[2]}: A verse section of hard rock with driving electric guitars and "
        "drums, raw energetic female vocal delivery."
    )
    result = check(qwen_s1, duration=5.665, expect_instruction=True, forbid_dialogue=True,
                   reference_slots={"picture": 2, "video": 0, "audio": 1})
    assert result.well_formed, [problem.message for problem in result.fatal]
    assert not any("cut time" in problem.message or "00:0" in problem.message
                   for problem in result.problems), [p.message for p in result.problems]
    assert [problem.fatal for problem in result.problems] == [False, False]
    # And with no slot counts to compare against, the prompt raises nothing at all.
    assert check(qwen_s1, duration=5.665, expect_instruction=True,
                 forbid_dialogue=True).problems == []


@pytest.mark.parametrize("word", ["At", "at", "AT", "aT"])
def test_every_casing_of_a_stray_cut_time_is_caught(word: str) -> None:
    """The defect was that exactly one of these four was seen. The timecode itself is
    unambiguous, so the casing in front of it cannot be what decides whether it is a cut."""
    problems = check_orphan_cuts(f"[Shot 1] She stands still. {word} 00:02.500 A wolf steps in.")
    assert [problem.fatal for problem in problems] == [True]
    assert f"'{word} 00:02.500' has no [Shot N] in front of it" in problems[0].message


@pytest.mark.parametrize("word", ["At", "at", "AT", "aT"])
def test_every_casing_of_a_marked_cut_time_belongs_to_its_shot(word: str) -> None:
    """The other half, and the one that would turn the fix into a false-positive machine.

    `_SHOT` and `_ANY_CUT` decide "does this time belong to a marker?" by comparing offsets, so
    they must tokenize a cut time identically. If only `_ANY_CUT` learned the new casings, every
    `[Shot 2] at 00:05.000` would be reported as an orphan *and* as a shot with no cut time —
    two fatal problems about a correct prompt. They are built from one shared fragment for this
    reason.
    """
    description = f"[Shot 1] A street. [Shot 2] {word} 00:05.000 A door opens."
    assert check_orphan_cuts(description, duration=8.0) == []
    assert check_shots(description, duration=8.0) == []
    # And the marked time is still bounds- and order-checked in every casing.
    assert any("at or beyond" in problem.message
               for problem in check_shots(description, duration=3.0))
    assert any("does not advance" in problem.message for problem in check_shots(
        f"[Shot 1] A street. [Shot 2] {word} 00:05.000 A door. [Shot 3] {word} 00:04.000 A cat."
    ))


def test_a_cut_time_is_not_found_inside_a_word() -> None:
    """`\\b` in front of the `at`. Otherwise "format 00:05.000" ends in a cut time, and the
    leniency that closes one hole opens a sillier one."""
    assert check_orphan_cuts("[Shot 1] Rendered in format 00:05.000 throughout.") == []


@pytest.mark.parametrize("name", ["overall_soundscape", "non_diegetic_music"])
def test_a_stray_cut_time_in_a_sound_field_is_reported_against_that_field(name: str) -> None:
    """The second gap the live instance exposed: the scan read the description only, so two of
    the three fields were exempt. H3 reads no timing out of a sound field at all, so a
    timestamp there cuts nothing — it is prose in the field least able to carry it."""
    prompt = (
        f"{CORE_FIELDS[0]}: [Shot 1] She stands at the mic.\n"
        f"{CORE_FIELDS[1]}: "
        + ("Room tone hums; at 00:01.500 a bulb buzzes." if name == CORE_FIELDS[1]
           else "Room tone hums quietly.") + "\n"
        f"{CORE_FIELDS[2]}: "
        + ("Guitars build; at 00:01.500 the drums enter." if name == CORE_FIELDS[2]
           else NOT_APPLICABLE)
    )
    result = check(prompt, duration=4.0)
    assert not result.well_formed
    flagged = [problem for problem in result.fatal if problem.field == name]
    assert [problem.field for problem in result.fatal] == [name]
    assert f"'at 00:01.500' is a cut time written into {name}" in flagged[0].message
    assert CORE_FIELDS[0] in flagged[0].message  # names where the timing does belong


def test_a_stray_cut_past_the_clips_end_is_its_own_problem_not_the_orphan_one() -> None:
    """Two defects, two sentences, deliberately not folded together.

    An orphan says a marker is missing and the fix is `[Shot N]`. A time past the end says the
    model has lost the clip's length and the fix is a different number — a cut that would still
    be wrong after a marker was written in front of it. The captured instance was both, and
    reporting either alone would send the retry loop after half the problem.
    """
    both = check_orphan_cuts("[Shot 1] She stands. at 00:06.250 She turns.", duration=5.665)
    assert len(both) == 2
    assert "has no [Shot N] in front of it" in both[0].message
    assert "at or beyond the 5.67s" in both[1].message
    assert "no such moment in the clip" in both[1].message
    assert [problem.fatal for problem in both] == [True, True]
    # Inside the clip: orphan only. The two are independent, not a pair.
    inside = check_orphan_cuts("[Shot 1] She stands. at 00:02.500 She turns.", duration=5.665)
    assert len(inside) == 1
    # "At or beyond", the same bound `check_shots` uses: a time equal to the clip's length is
    # one frame past its last, so it names no moment the render contains.
    edge = check_orphan_cuts("[Shot 1] She stands. at 00:04.000 She turns.", duration=4.0)
    assert len(edge) == 2
    assert "no such moment in the clip" in edge[1].message
    # No duration to bound against: orphan only, and no invented ceiling.
    assert len(check_orphan_cuts("[Shot 1] She stands. at 00:06.250 She turns.")) == 1
    # A time a shot marker *does* claim is bounded by `check_shots` and is not reported here
    # a second time; one defect, one sentence.
    assert check_orphan_cuts("[Shot 1] A street. [Shot 2] At 00:09.000 A door.",
                             duration=3.75) == []


@pytest.mark.parametrize(("open_tag", "close_tag"), [("<d>", "</d>"), ("<D>", "</D>")])
def test_dialogue_tags_are_read_in_either_casing(open_tag: str, close_tag: str) -> None:
    """Found by the audit, not by a bad render, which is the point of auditing.

    A `<D>` block defeated all three dialogue checks at once: the balance count saw zero tags
    so they balanced, the language-tag scan found no block to look at, and — worst — the
    `forbid_dialogue` guard did not fire. That guard exists because on 2026-08-19 the model
    invented well-formed lyrics onto a shot riding the master song. A shift key walked through
    it.
    """
    assert any("no language tag" in problem.message
               for problem in check_dialogue(f"[Shot 1] She says {open_tag}Hi.{close_tag}"))
    assert any("unbalanced" in problem.message
               for problem in check_dialogue(f"[Shot 1] She says {open_tag}[English] Hi."))
    # A correctly tagged block in either casing still raises nothing about its language.
    assert check_dialogue(f"[Shot 1] {open_tag}[English] Hi.{close_tag}") == []
    song_audio = (
        f"{CORE_FIELDS[0]}: [Shot 1] She stands at the mic. "
        f'{open_tag}[English] "Words from nowhere"{close_tag}\n\n'
        f"{CORE_FIELDS[1]}: {NOT_APPLICABLE}\n\n"
        f"{CORE_FIELDS[2]}: <Audio 1> is directly reused as the complete audience-only score."
    )
    assert any("no <d> dialogue block" in problem.message
               for problem in check(song_audio, duration=4.0, forbid_dialogue=True).problems)


@pytest.mark.parametrize("speaker", ["(S1)", "(s1)", "(S1, s2)"])
def test_a_speaker_id_in_retention_analysis_is_caught_in_either_casing(speaker: str) -> None:
    problems = check_retention(
        f"retention_analysis:\n<Subject 1> {speaker} fully_preserved - face kept.\n"
    )
    assert problems and "belong in the description" in problems[0].message


def test_the_retention_label_itself_is_read_in_any_casing() -> None:
    """A mis-cased `Retention_Analysis:` used to skip the check in silence, which is the
    failure mode worth closing. The core field labels are deliberately *not* treated this way:
    a mis-cased one is refused outright as "no core fields found", and `_FIELD_LINE` — the one
    pattern here that rewrites payload bytes — has to keep reading exactly what `parse` does."""
    assert check_retention("Retention_Analysis:\n<Subject 1> (S1) fully_preserved.\n")
    assert check_retention("RETENTION_ANALYSIS:\n<Subject 1> (S1) fully_preserved.\n")
    # And the core fields stay exact: a mis-cased label is a loud refusal, not a quiet pass.
    mis_cased = (
        "Integrated_Multimodal_Description: [Shot 1] A street at dawn.\n"
        "Overall_Soundscape: Traffic hums.\n"
        f"Non_Diegetic_Music: {NOT_APPLICABLE}"
    )
    result = check(mis_cased)
    assert not result.well_formed
    assert any("No core fields found" in problem.message for problem in result.fatal)


def test_the_gemma_outputs_captured_that_day_still_raise_nothing() -> None:
    """The other side of the blast radius. Gemma's five measured shots raised zero problems
    before this change and must raise zero after it — the widened scan has to be invisible to
    a prompt that never made the mistake. Two of them, quoted from
    `run6-expansions-gemma.json`."""
    s5 = (
        f"{CORE_FIELDS[0]}: [Shot 1] LOW STATIC WIDE of the canopy bed with the chrome mic "
        "stand abandoned in the foreground; a figure crosses the far end of the room and is "
        "gone. Moonlight rakes across bare concrete.\n"
        f"{CORE_FIELDS[1]}: The warehouse settles into stillness, a faint electrical hum from "
        "the bare bulb overhead. Distant wind rattles the corrugated walls.\n"
        f"{CORE_FIELDS[2]}: Hard rock winds down, driving guitars thinning to a single "
        "sustained chord."
    )
    assert check(s5, duration=5.0, reference_slots={"picture": 0, "video": 0,
                                                    "audio": 0}).problems == []
    warmup = (
        f"{CORE_FIELDS[0]}: [Shot 1] A moonlit warehouse interior, wide static frame, dust "
        "motes drifting in a current of air beneath a single bare bulb.\n"
        f"{CORE_FIELDS[1]}: The room hums low as moonlight filters through high windows.\n"
        f"{CORE_FIELDS[2]}: Electric guitars and pounding drums, raw 80s metal energy."
    )
    assert check(warmup, duration=4.0).problems == []
