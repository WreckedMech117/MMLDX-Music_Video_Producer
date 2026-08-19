import hashlib
import json

import pytest

from music_video_producer.models import (
    AssetCitation,
    Project,
    Shot,
    Song,
    citations_in_prompt_order,
)
from music_video_producer.timeline import (
    TimelineError,
    align_h3_frames,
    build_director_timeline,
    expansion_input,
    ordered_shots,
    shot_expansion_input,
    song_section,
)


def test_align_h3_frames_uses_17k_plus_5_grid():
    assert align_h3_frames(120) == 124
    assert align_h3_frames(124) == 124
    assert align_h3_frames(125) == 141


def test_director_timeline_sorts_shots_and_uses_frames():
    shots = [
        Shot(start=3.0, duration=2.0, prompt="close-up"),
        Shot(start=0.0, duration=3.0, prompt="wide shot"),
    ]

    result = build_director_timeline(shots, window_start=0, window_duration=5, fps=24)
    payload = json.loads(result.timeline_data)

    assert [segment["prompt"] for segment in payload["segments"]] == ["wide shot", "close-up"]
    assert payload["segments"][1]["start"] == 72
    assert payload["segments"][1]["length"] == 48
    assert result.requested_frames == 120
    assert result.aligned_frames == 124
    assert result.warnings == []


def test_director_timeline_rejects_overlapping_shots():
    shots = [
        Shot(start=0, duration=4, prompt="first"),
        Shot(start=3, duration=4, prompt="second"),
    ]

    with pytest.raises(TimelineError, match="overlap"):
        build_director_timeline(shots, window_start=0, window_duration=7)


def test_director_timeline_warns_outside_h3_training_window():
    result = build_director_timeline(
        [Shot(start=0, duration=18, prompt="long")], window_start=0, window_duration=18
    )

    assert any("4–15" in warning for warning in result.warnings)


# A lyric sheet with the three things a real one has and nothing in this path may touch:
# section tags, interior blank lines, and indentation. Written for this suite rather than taken
# from a real song — a copyrighted sheet is not test data, and nothing here depends on the words.
SPINE_LYRIC_SHEET = (
    "[Verse 1]\n"
    "Cold rail, the platform hums\n"
    "\n"
    "    a paper cup goes over the edge\n"
    "\n"
    "[Chorus]\n"
    "Hold the line, hold the line\n"
    "\n"
    "[Bridge]\n"
    "    counting sodium lights"
)
SPINE_SONG_STYLE = "Downtempo industrial pop, close female vocal, tape saturation, no live drums."


def expansion_project(*, song: Song | None) -> Project:
    """A plan whose shots are deliberately out of manifest order, with one locked."""
    project = Project(name="Expansion")
    project.creative_brief = "A performer crosses from confinement into open desert."
    project.treatment = "Three movements: the corridor, the threshold, the desert."
    project.style_bible = "Sodium amber, hard backlight, 35mm grain."
    project.song = song
    project.shots = [
        Shot(id="shot_middle", start=30, duration=6, prompt="Threshold"),
        Shot(id="shot_first", start=0, duration=5, prompt="Corridor"),
        Shot(id="shot_last", start=90, duration=20, prompt="Desert", locked=True),
    ]
    return project


def test_expansion_input_carries_each_shots_position_in_the_song():
    """The acceptance criterion is asserted against the builder, not an incidental field.

    The chat route already dumps every Shot's start, duration and end, so "the input includes
    each Shot's position" would be satisfied by doing nothing. Position here means the ordered
    index, the absolute seconds, and the fraction of the song — computed, and asserted here.
    """
    built = expansion_input(expansion_project(song=Song(title="Spine", source="imported", duration=120)))

    # Ordered by start, so `index` is the Shot's place in the song and not in the manifest.
    assert [shot["shot_id"] for shot in built["shots"]] == ["shot_first", "shot_middle", "shot_last"]
    assert [shot["index"] for shot in built["shots"]] == [0, 1, 2]
    assert built["shots"][1]["start"] == 30
    assert built["shots"][1]["end"] == 36
    assert built["shots"][1]["duration"] == 6
    assert built["shots"][1]["song_fraction"] == 0.25
    # Exact equality, still: the block is a *song* block and nothing else may drift into it.
    # This Song carries no words and no style description, so the payload is the one this
    # builder produced before either field existed — absent, never `""`.
    assert built["song"] == {"title": "Spine", "duration": 120}
    # The documents the prompts have to embed, and nothing about takes or renders.
    assert built["treatment"].startswith("Three movements")
    assert built["style_bible"].startswith("Sodium amber")
    # A locked Shot stays in the plan — the model needs the whole through-line — and is flagged.
    assert built["shots"][2]["locked"] is True
    assert built["shots"][0]["locked"] is False
    # Descriptive window flags over the project's own Shots: the 20 s Shot is outside H3's
    # reliable range, the 5 s one is not. Nothing here gates anything; expansion writes prompts.
    assert built["shots"][2]["outside_h3_window"] is True
    assert built["shots"][0]["outside_h3_window"] is False


def test_expansion_input_carries_the_songs_words_and_style_exactly_as_stored():
    """The words reach the planning act most likely to want them, and reach it unaltered.

    Pinned by exact equality rather than by `in`, for the same reason the title-and-duration
    assertion above always was: this block is a *song* block, and the guard that catches the
    next unintended widening of it is the one that names every key it may hold. A looser check
    would pass just as happily on a block that had grown a fourth field nobody decided on.

    Interior structure is asserted separately from the equality, because equality alone would
    still hold if the sheet were normalised on both sides of this test at once — the constant
    is checked to actually contain a section tag, a blank line and an indent, so "exactly as
    stored" is a claim about something rather than about an already-flat string.
    """
    song = Song(
        title="Spine",
        source="imported",
        duration=120,
        lyrics=SPINE_LYRIC_SHEET,
        caption=SPINE_SONG_STYLE,
    )

    built = expansion_input(expansion_project(song=song))

    assert built["song"] == {
        "title": "Spine",
        "duration": 120,
        "lyrics": SPINE_LYRIC_SHEET,
        "caption": SPINE_SONG_STYLE,
    }
    # No parsing, sectioning, excerpting or summarising: the sheet goes whole.
    assert "[Chorus]" in built["song"]["lyrics"]
    assert "\n\n" in built["song"]["lyrics"]
    assert "\n    counting sodium lights" in built["song"]["lyrics"]
    # A maximum sheet is sent whole too — this is one stateless call, not a growing thread.
    maximum = Song(
        title="Long", source="imported", duration=120, lyrics="x" * 8000, caption="y" * 4000
    )
    whole = expansion_input(expansion_project(song=maximum))["song"]
    assert len(whole["lyrics"]) == 8000
    assert len(whole["caption"]) == 4000


def test_expansion_input_omits_the_songs_words_and_style_when_the_song_has_none():
    """Absent means absent, so a song with neither is byte-identical to what it was before.

    `""` is not "this song has no words" — it is a confident claim that it has none, which is
    the same failure a fabricated `song_fraction` of 0.0 would be. Each of the four
    combinations is pinned by exact equality, so a field emptied instead of omitted fails here
    whichever of the two it is, and the serialised form is compared against a Song built
    without the fields at all — which is what "byte-identical" actually means.
    """
    bare = Song(title="Spine", source="imported", duration=120)
    cases = {
        "neither": (bare, {"title": "Spine", "duration": 120}),
        "words only": (
            bare.model_copy(update={"lyrics": SPINE_LYRIC_SHEET}),
            {"title": "Spine", "duration": 120, "lyrics": SPINE_LYRIC_SHEET},
        ),
        "style only": (
            bare.model_copy(update={"caption": SPINE_SONG_STYLE}),
            {"title": "Spine", "duration": 120, "caption": SPINE_SONG_STYLE},
        ),
        "blank strings": (
            bare.model_copy(update={"lyrics": "", "caption": ""}),
            {"title": "Spine", "duration": 120},
        ),
    }
    for label, (song, expected) in cases.items():
        built = expansion_input(expansion_project(song=song))

        assert built["song"] == expected, label
        for field in ("lyrics", "caption"):
            if field not in expected:
                assert field not in built["song"], (label, field)

    # Byte-identical, not merely equal: the whole payload for a Song carrying neither field is
    # the exact JSON this builder produced before either field existed.
    without = json.dumps(expansion_input(expansion_project(song=bare)), sort_keys=True)
    assert '"song": {"duration": 120.0, "title": "Spine"}' in without
    assert "lyrics" not in without
    assert "caption" not in without
    # And no Song at all is still no `song` key, unchanged by any of the above.
    assert "song" not in expansion_input(expansion_project(song=None))


def test_expansion_input_omits_the_song_fraction_rather_than_fabricating_zero():
    """No Song, and a Song of unknown length, are both "absent" — never 0.0.

    A fabricated 0.0 tells the model every shot opens the song, which is worse than telling it
    nothing: it is confidently wrong direction that would flatten the energy curve FR-26 asks
    for. Absolute seconds and the ordered index still carry the position.
    """
    for label, song in (
        ("no song", None),
        ("unknown length", Song(title="Unmeasured", source="imported", duration=0)),
    ):
        built = expansion_input(expansion_project(song=song))

        for shot in built["shots"]:
            assert "song_fraction" not in shot, (label, shot["shot_id"])
            # Position is still carried, so the absence costs the model nothing it could have had.
            assert shot["start"] >= 0
            assert "index" in shot
    assert "song" not in expansion_input(expansion_project(song=None))


def test_expansion_input_omits_the_section_because_no_analyser_exists():
    """FR-26's "section boundaries when analysis exists" has no data source in this project.

    Asserted two ways so the empty branch cannot quietly become a fabrication: the key is absent
    from every shot, and no model anywhere carries the section or tempo data one would be
    derived from. If an analyser ever lands, `song_section` is the one place that changes and
    this test is what says the slot was empty rather than forgotten.
    """
    built = expansion_input(expansion_project(song=Song(title="Spine", source="imported", duration=120)))

    for shot in built["shots"]:
        assert "section" not in shot, shot["shot_id"]
    assert song_section(Project(name="Any"), Shot(start=0, duration=5)) == ""
    fields = set(Song.model_fields) | set(Project.model_fields) | set(Shot.model_fields)
    for absent in ("section", "sections", "bpm", "tempo", "beats", "structure"):
        assert absent not in fields, absent


def test_expansion_input_names_each_shots_neighbours_without_repeating_their_prompts():
    """Cross-shot variance needs each Shot to know what it cuts from and into.

    Adjacency and the neighbour's window, not the neighbour's prompt. On a first expansion —
    the primary case — every prompt is "" or the "New shot" placeholder, so carrying it would
    convey nothing exactly when the variance mechanism matters most, while shipping every
    prompt three times against a payload whose whole justification is that it is trimmed. The
    full entry is reachable by id, and by `index ± 1` because the list is ordered.
    """
    built = expansion_input(expansion_project(song=None))
    first, middle, last = built["shots"]

    assert first["neighbours"] == {"next": {"shot_id": "shot_middle", "start": 30, "end": 36}}
    assert middle["neighbours"] == {
        "previous": {"shot_id": "shot_first", "start": 0, "end": 5},
        "next": {"shot_id": "shot_last", "start": 90, "end": 110},
    }
    assert last["neighbours"] == {"previous": {"shot_id": "shot_middle", "start": 30, "end": 36}}
    # Each prompt appears exactly once in the payload, as its own shot's `current_prompt`.
    assert json.dumps(built).count("Threshold") == 1
    # And a neighbour id is a real entry in `shots`, so nothing is lost by not repeating it.
    ids = {shot["shot_id"] for shot in built["shots"]}
    for shot in built["shots"]:
        for framing in shot["neighbours"].values():
            assert framing["shot_id"] in ids


def test_expansion_input_clamps_the_song_fraction_to_the_song():
    """Guidance, not geometry. A Shot can legitimately sit past a shorter song's end.

    Nothing retimes Shots when the Song changes — that is what the replacement gate promises —
    so a 200 s plan against a 120 s song is an ordinary state, and telling the model a Shot sits
    at 1.67 of the way through the song is not something it can use. The absolute seconds are
    left alone and still carry the real timing.
    """
    project = Project(name="Past the end")
    project.song = Song(title="Short", source="imported", duration=100)
    project.shots = [
        Shot(id="shot_inside", start=25, duration=5, prompt="Inside"),
        Shot(id="shot_past", start=250, duration=5, prompt="Past the end"),
    ]
    # `Shot.start` is `ge=0`, so a negative offset cannot be constructed through validation;
    # `model_construct` bypasses it to prove the clamp is defence in depth rather than a comment.
    project.shots.insert(0, Shot.model_construct(id="shot_before", start=-30.0, duration=5.0))

    fractions = {shot["shot_id"]: shot["song_fraction"] for shot in expansion_input(project)["shots"]}

    assert fractions["shot_before"] == 0.0
    assert fractions["shot_inside"] == 0.25
    assert fractions["shot_past"] == 1.0
    # The unclamped truth is still there, in the field that is a measurement.
    starts = {shot["shot_id"]: shot["start"] for shot in expansion_input(project)["shots"]}
    assert starts["shot_past"] == 250


def test_the_model_and_the_notices_are_numbered_by_the_same_ordering():
    """One ordering, exported, so the input and the reply cannot number a Shot differently.

    `expansion_input` orders by `start`; the timeline draws clips in manifest order. A route
    that numbered its notices by the manifest would call a Shot "shot 03" while telling the
    model it was "index 1" — for exactly the plans this fixture builds, where the two differ.
    """
    project = expansion_project(song=None)

    assert [shot.id for shot in ordered_shots(project)] == [
        "shot_first",
        "shot_middle",
        "shot_last",
    ]
    # Deliberately not the manifest order, or this test would be vacuous.
    assert [shot.id for shot in project.shots] != [shot.id for shot in ordered_shots(project)]
    built = expansion_input(project)
    for index, shot in enumerate(ordered_shots(project)):
        assert built["shots"][index]["shot_id"] == shot.id
        assert built["shots"][index]["index"] == index
    # Stable for equal starts: two Shots at the same second keep their manifest order rather
    # than swapping between two calls over the same project.
    tied = Project(name="Tied")
    tied.shots = [Shot(id="shot_b", start=4, duration=2), Shot(id="shot_a", start=4, duration=2)]
    assert [shot.id for shot in ordered_shots(tied)] == ["shot_b", "shot_a"]


def test_expansion_input_of_a_single_shot_plan_has_no_neighbours():
    project = Project(name="Solo")
    project.shots = [Shot(id="shot_only", start=0, duration=5, prompt="One take")]

    built = expansion_input(project)

    assert built["shots"][0]["neighbours"] == {}
    assert built["shots"][0]["index"] == 0


def _expansion_project() -> Project:
    return Project(
        name="Two passes",
        treatment="A lone performer intercut with wild imagery.",
        style_bible="Sodium amber and deep blacks.",
        song=Song(
            title="Harder Faster",
            source="imported",
            duration=154.6,
            lyrics="[verse] there is a hunger",
            caption="mid-tempo metal",
        ),
        shots=[
            Shot(id="shot_a", start=0.0, duration=4.0, prompt="Wide on Lucy"),
            Shot(id="shot_b", start=12.0, duration=3.75, prompt="Wolf B-roll",
                 singing="not_singing"),
            Shot(id="shot_c", start=20.0, duration=4.0, prompt="Close on her face"),
        ],
    )


def test_the_per_shot_input_carries_the_neighbours_intents_not_their_expansions():
    """The whole reason this pass is per-Shot rather than whole-plan.

    Pass one withholds neighbour prompts because on a first pass they are placeholders. On
    this pass they are real, and a cut that lands well needs to know what it is cutting from
    — so intents are carried. Their *expansions* are not, and that is the line: two long-form
    prompts per call is exactly the bloat that makes one-shot-for-all impossible.
    """
    project = _expansion_project()
    project.shots[0].h3_prompt = "integrated_multimodal_description: [Shot 1] A long document."

    built = shot_expansion_input(project, project.shots[1])

    assert built["neighbours"]["previous"]["intent"] == "Wide on Lucy"
    assert built["neighbours"]["next"]["intent"] == "Close on her face"
    assert "A long document" not in json.dumps(built)
    assert "h3_prompt" not in json.dumps(built)


def test_the_per_shot_input_sends_the_whole_lyric_sheet_and_never_claims_it_is_the_window():
    """Nothing aligns lyrics to time, so "the words for this window" cannot be built.

    `song_section` is an empty branch for exactly this reason: there is no BPM or section
    field on any model and no analyser. Sending the sheet under a key claiming it was this
    clip's words would be a fabrication, so it goes as `lyrics` with `song_fraction` beside
    it as the honest signal of position, and the specialist's prompt tells the model the
    sheet is unaligned.
    """
    built = shot_expansion_input(_expansion_project(), _expansion_project().shots[1])

    assert built["song"]["lyrics"] == "[verse] there is a hunger"
    assert built["song"]["song_fraction"] == round(12.0 / 154.6, 4)
    assert not any("window" in key for key in built["song"])


def test_the_per_shot_input_numbers_the_reference_tags_the_model_may_use():
    """The prompt forbids inventing a tag, and a model told only "you have two pictures"
    will still guess at their numbers. Naming each tag beside its role removes the guess."""
    project = _expansion_project()
    project.shots[1].citations = [
        AssetCitation(asset_id="asset_2", role="last", order=1),
        AssetCitation(asset_id="asset_1", role="first", order=0),
    ]

    references = shot_expansion_input(project, project.shots[1])["shot"]["references"]

    assert [entry["tag"] for entry in references] == ["<Picture 1>", "<Picture 2>"]
    assert {entry["role"] for entry in references} == {"first frame", "last frame"}


def test_the_per_shot_input_numbers_mixed_roles_in_the_renders_own_walk():
    """Keyframe roles ahead of references, by `citations_in_prompt_order` — the render's walk.

    The tag this input tells the specialist to declare a role for must be the tag the payload's
    media order implies, because H3's slots are anonymous and `<Picture 1>` *is* slot one. The
    list order here is adversarial — a reference cited before the first frame, and an explicit
    `order` contradicting list position within the reference role — so an input numbered by
    list position, or by any key other than the shared one, produces a different list and fails.
    """
    project = _expansion_project()
    # The keyframe citation's order is the largest on purpose: under the shared `(role, order)`
    # key it still numbers first, while `(order, role)` or list position each number a
    # reference into <Picture 1> — so a drifted key fails rather than coinciding.
    project.shots[1].citations = [
        AssetCitation(asset_id="asset_stage", role="reference", order=1),
        AssetCitation(asset_id="asset_portrait", role="first", order=7),
        AssetCitation(asset_id="asset_wolf", role="reference", order=0),
    ]

    references = shot_expansion_input(project, project.shots[1])["shot"]["references"]

    assert [(entry["tag"], entry["role"], entry["asset_id"]) for entry in references] == [
        ("<Picture 1>", "first frame", "asset_portrait"),
        ("<Picture 2>", "reference", "asset_wolf"),
        ("<Picture 3>", "reference", "asset_stage"),
    ]
    # And the shared walk really is the one this numbering came from.
    assert [entry["asset_id"] for entry in references] == [
        citation.asset_id
        for citation in citations_in_prompt_order(project.shots[1])
    ]


#: `shot_expansion_input`, digested at commit `a754794` — the keyframes-in-references baseline —
#: over the two pre-existing shapes that story was forbidden to move: a reference-only shot and
#: a dedicated first/last keyframe shot. The story rerouted this builder's numbering through
#: `citations_in_prompt_order`, and for these shapes that function must be the identity of the
#: old inline sort; a digest is the only assertion that cannot drift with the code it checks.
EXPANSION_INPUT_REFERENCE_ONLY_DIGEST = (
    "30fea20f3276e3fade1c567df8b469a6d36e859bf8e1f22efbb36848cc46f496"
)
EXPANSION_INPUT_FIRST_LAST_DIGEST = (
    "a4a612e564402b8c114f63421af8fe04290b0fcc2bf32d9785a8a1b0c8e88354"
)


def test_pre_existing_expansion_inputs_are_byte_identical_across_the_keyframe_change():
    """The spec's byte-identity rail, on the expansion channel.

    Everything here is pinned — ids, times, the song — so the digest moves only if the payload
    does. If one of these fails, a pre-existing shape's expansion input changed; re-deriving
    the digest is the wrong fix unless the Director has renegotiated that promise.
    """
    project = Project(
        id="project_pinned0001", name="Pinned",
        creative_brief="brief", treatment="treatment", style_bible="style",
        song=Song(title="Harder Faster", source="imported", duration=154.6,
                  lyrics="[verse] there is a hunger", caption="mid-tempo metal"),
        shots=[
            Shot(id="shot_refonly", start=12.0, duration=3.75, prompt="Wolf B-roll",
                 singing="not_singing",
                 citations=[AssetCitation(asset_id="asset_b", role="reference", order=1),
                            AssetCitation(asset_id="asset_a", role="reference", order=0)]),
            Shot(id="shot_fl", start=20.0, duration=4.0, prompt="Close on her face",
                 mode="first_last",
                 citations=[AssetCitation(asset_id="asset_2", role="last", order=1),
                            AssetCitation(asset_id="asset_1", role="first", order=0)]),
        ],
    )

    digests = {
        shot.id: hashlib.sha256(
            json.dumps(
                shot_expansion_input(project, shot), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        for shot in project.shots
    }

    assert digests["shot_refonly"] == EXPANSION_INPUT_REFERENCE_ONLY_DIGEST
    assert digests["shot_fl"] == EXPANSION_INPUT_FIRST_LAST_DIGEST


def test_the_per_shot_input_omits_what_a_shot_does_not_have():
    """Absent rather than empty, the same discipline the whole-plan builder already keeps."""
    project = Project(
        name="Bare",
        shots=[Shot(id="only", start=0.0, duration=4.0, prompt="A street at dawn")],
    )

    built = shot_expansion_input(project, project.shots[0])

    assert "song" not in built
    assert "neighbours" not in built
    assert "references" not in built["shot"]
    assert built["shot"]["index"] == 1 and built["shot"]["of"] == 1


def test_the_per_shot_input_refuses_a_shot_from_another_project():
    project = _expansion_project()
    with pytest.raises(TimelineError):
        shot_expansion_input(project, Shot(id="elsewhere", start=0.0, duration=4.0))
