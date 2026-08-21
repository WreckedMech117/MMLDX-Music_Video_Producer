import hashlib
import json
from itertools import pairwise

import pytest

from music_video_producer.assembly import ClipWindow, tiling_refusals
from music_video_producer.models import (
    Asset,
    AssetCitation,
    Project,
    Shot,
    Song,
    SongSection,
    VisionInspectionRecord,
    citations_in_prompt_order,
    song_audio_tag,
)
from music_video_producer.timeline import (
    H3_FPS,
    H3_MAX_SHOT_SECONDS,
    H3_MIN_RENDER_FRAMES,
    H3_MIN_SHOT_SECONDS,
    OVER_RENDER_LEAD_SECONDS,
    OVER_RENDER_SECONDS,
    SNAP_ALREADY_SILENT,
    SNAP_APPROVED_REFUSAL,
    SNAP_CLEARANCE_SECONDS,
    SNAP_CONTIGUITY_TOLERANCE,
    SNAP_IN_FLIGHT_REFUSAL,
    SNAP_LOCKED_REFUSAL,
    SNAP_MINIMUM_GAP_SECONDS,
    SNAP_NO_GAP_IN_TOLERANCE,
    SNAP_OUT_OF_BAND,
    SNAP_TOLERANCE_DEFAULT,
    SNAP_TOLERANCE_OFF,
    SNAP_UNMEASURED,
    SNAP_WITHOUT_CUTS,
    TimelineError,
    _asset_description,
    _gap_snap_target,
    align_h3_frames,
    anchored_label,
    asset_anchor,
    assistant_input,
    build_director_timeline,
    expansion_input,
    margin_frames,
    ordered_shots,
    over_render_centred,
    over_render_frames,
    over_render_lead,
    populate_windows,
    proposal_for_position,
    repair_sections,
    section_lyrics,
    shot_expansion_input,
    snap_cut_plan,
    song_section,
    vocal_gaps,
)
from music_video_producer.transcription import merge_vocal_spans


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


def test_expansion_input_omits_the_section_when_nothing_is_marked():
    """Absence still means unknown — the slot's rule survives the slot being filled.

    The data source arrived on 2026-08-19 (the Director's own `Project.sections`, not an
    analyser), so the claim shrinks but does not invert: a project with no marks carries
    no `section` key anywhere, because an absent value must never reach the model as a
    confident one.
    """
    built = expansion_input(expansion_project(song=Song(title="Spine", source="imported", duration=120)))

    for shot in built["shots"]:
        assert "section" not in shot, shot["shot_id"]
    assert song_section(Project(name="Any"), Shot(start=0, duration=5)) is None
    # Tempo remains genuinely unsourced; sections are now the Director's field.
    fields = set(Song.model_fields) | set(Project.model_fields) | set(Shot.model_fields)
    for absent in ("bpm", "tempo", "beats", "structure"):
        assert absent not in fields, absent
    assert "sections" in Project.model_fields


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


def test_the_per_shot_input_carries_no_lyric_text_anywhere():
    """The Director's 2026-08-19 ruling, measured twice on live renders: given lyric text,
    the model plants it into the wrong windows (verse-one words at 4 s when the vocal
    starts at 13 s), and words in the prompt fight the audio reference that actually
    drives the mouth. So no key of this payload carries song words — not the song block,
    not the section block — while the caption (how the track sounds) and `song_fraction`
    (where the shot sits) stay as the honest, word-free signals.
    """
    built = shot_expansion_input(_expansion_project(), _expansion_project().shots[1])

    assert "lyrics" not in built["song"]
    assert "there is a hunger" not in json.dumps(built)
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


def test_the_per_shot_input_names_the_master_songs_audio_tag_for_song_audio_shots():
    """The lipsync handle (2026-08-19): the creator's own working music-video prompts
    drive the mouth by naming the audio tag in the description — "the vocal in <Audio 1>
    drives her lip movements" — so a song-audio shot's payload must tell the specialist
    the tag exists and what number the render will give it. Same walk as the render
    (`song_audio_tag`), same reason as the picture tags: no guessed numbers."""
    project = _expansion_project()
    project.shots[1].use_song_audio = True
    project.shots[1].citations = [AssetCitation(asset_id="asset_1", role="reference", order=0)]

    references = shot_expansion_input(project, project.shots[1])["shot"]["references"]

    assert references[-1]["tag"] == "<Audio 1>"
    assert "master song" in references[-1]["role"]
    # And a shot not riding the song gets no audio tag it could cite.
    project.shots[1].use_song_audio = False
    references = shot_expansion_input(project, project.shots[1])["shot"]["references"]
    assert all("Audio" not in entry["tag"] for entry in references)


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


def _mixed_kinds_project() -> Project:
    """One project holding a picture, a video, an audio and two keyframe images."""
    project = _expansion_project()
    project.assets = [
        Asset(id="asset_stage", name="Stage", kind="setting", path="stage.png"),
        Asset(id="asset_pan", name="Camera pan", kind="video", path="pan.mp4"),
        Asset(id="asset_room", name="Room tone", kind="audio", path="room.flac"),
        Asset(id="asset_open", name="Opening frame", kind="image", path="open.png"),
        Asset(id="asset_close", name="Closing frame", kind="image", path="close.png"),
    ]
    return project


def test_a_picture_only_shots_expansion_input_keeps_the_one_picture_series():
    """The byte-identity rail of the per-kind numbering change, stated as the old rule.

    Before 2026-08-20 this builder ran its own counter and numbered *every* citation into the
    `<Picture N>` series. For a shot citing only pictures — which is every shot this
    application produces today — the shared per-kind rule must produce that same series,
    position for position, or a working project's expansion inputs would all move. The two
    pinned digests below are the same promise in bytes; this is it in words, so a reader can
    see what the digests are protecting.
    """
    project = _mixed_kinds_project()
    project.shots[1].citations = [
        AssetCitation(asset_id="asset_stage", role="reference", order=1),
        AssetCitation(asset_id="asset_open", role="reference", order=0),
        # A citation this project does not hold still numbers, exactly as it always has: the
        # render refuses it by name, and dropping it here would renumber its neighbours.
        AssetCitation(asset_id="asset_gone", role="reference", order=2),
    ]

    references = shot_expansion_input(project, project.shots[1])["shot"]["references"]

    assert [entry["tag"] for entry in references] == [
        f"<Picture {position}>"
        for position, _ in enumerate(citations_in_prompt_order(project.shots[1]), start=1)
    ]


def test_the_expansion_input_numbers_a_video_and_an_audio_into_their_own_series():
    """The defect this change exists for (found 2026-08-20).

    A shot citing a video used to be told `<Picture 2>` for the slot the payload wires as
    `<Video 1>` — the specialist then writes a prompt naming a slot that does not hold what it
    claims, and H3's slots are anonymous, so the take comes back plausible and wrong. Three
    independent counters, interleaved on purpose: the picture after the video is `<Picture 2>`
    and not `<Picture 3>`.
    """
    project = _mixed_kinds_project()
    project.shots[1].citations = [
        AssetCitation(asset_id="asset_open", role="reference", order=0),
        AssetCitation(asset_id="asset_pan", role="reference", order=1),
        AssetCitation(asset_id="asset_stage", role="reference", order=2),
        AssetCitation(asset_id="asset_room", role="reference", order=3),
    ]

    references = shot_expansion_input(project, project.shots[1])["shot"]["references"]

    assert [entry["tag"] for entry in references] == [
        "<Picture 1>", "<Video 1>", "<Picture 2>", "<Audio 1>"
    ]


def test_the_expansion_input_gives_the_master_song_the_slot_after_every_cited_audio():
    """The song is appended last by the render, so its tag is one past the cited audios.

    A shot citing two audio assets and riding the song is told `<Audio 3>` for the song — the
    number `models.song_audio_tag` computes and the submit route writes into its own map. Under
    the old single-series numbering the cited audios were pictures, so the song's tag was
    `<Audio 1>` while the payload wired it third.
    """
    project = _mixed_kinds_project()
    project.assets.append(Asset(id="asset_hum", name="Hum", kind="audio", path="hum.flac"))
    project.shots[1].use_song_audio = True
    project.shots[1].citations = [
        AssetCitation(asset_id="asset_room", role="reference", order=0),
        AssetCitation(asset_id="asset_hum", role="reference", order=1),
        AssetCitation(asset_id="asset_stage", role="reference", order=2),
    ]

    references = shot_expansion_input(project, project.shots[1])["shot"]["references"]

    assert [entry["tag"] for entry in references] == [
        "<Audio 1>", "<Audio 2>", "<Picture 1>", "<Audio 3>"
    ]
    assert references[-1]["role"].startswith("master song")
    assert references[-1]["tag"] == f"<Audio {song_audio_tag(project, project.shots[1])}>"


def test_keyframe_roles_number_into_the_picture_series_beside_a_cited_video():
    """A frame is a picture, whatever else the shot cites.

    `first` and `last` take `<Picture 1>` and `<Picture 2>` here even though a video is cited
    between them, because the payload wires a keyframe as an ordinary picture slot and only its
    tag line says which frame it is. A keyframe numbered into the video series would name a slot
    holding the pan.
    """
    project = _mixed_kinds_project()
    project.shots[1].citations = [
        AssetCitation(asset_id="asset_pan", role="reference", order=0),
        AssetCitation(asset_id="asset_close", role="last", order=1),
        AssetCitation(asset_id="asset_open", role="first", order=0),
    ]

    references = shot_expansion_input(project, project.shots[1])["shot"]["references"]

    assert [(entry["tag"], entry["role"]) for entry in references] == [
        ("<Picture 1>", "first frame"),
        ("<Picture 2>", "last frame"),
        ("<Video 1>", "reference"),
    ]


#: `shot_expansion_input`, digested at commit `a754794` — the keyframes-in-references baseline —
#: over the two pre-existing shapes that story was forbidden to move: a reference-only shot and
#: a dedicated first/last keyframe shot. The story rerouted this builder's numbering through
#: `citations_in_prompt_order`, and for these shapes that function must be the identity of the
#: old inline sort; a digest is the only assertion that cannot drift with the code it checks.
#:
#: Re-derived 2026-08-19 under the Director's renegotiation of the payload itself: lyric text
#: no longer rides any per-shot expansion (measured twice — given words, the model plants them
#: into wrong windows and the text fights the audio reference that drives the mouth), so the
#: `song` block lost its `lyrics` key and the digests moved with it.
EXPANSION_INPUT_REFERENCE_ONLY_DIGEST = (
    "cb26a873fb86206be8e7a880617f02cb56f10003682d9a47650bf55cac2b8698"
)
EXPANSION_INPUT_FIRST_LAST_DIGEST = (
    "7cba3a4c502919991883becfe4d02d9f0f3a582c8c81aec7029a428d06cd0472"
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


# --- The over-render margin (spec-monitor-and-over-render) --------------------------------


def test_over_render_frames_is_never_exact_or_lesser_than_the_window():
    """The Director's ruling, in frames: a 3.75 s window renders 107 frames (4.458 s), a
    4 s window 124, an 8 s window 209 — and across the whole plannable range the take is
    always longer than the window by at least the margin, less the half frame rounding
    can shave, and still on the 17k+5 grid."""
    assert over_render_frames(3.75) == 107
    assert over_render_frames(4.0) == 124
    assert over_render_frames(5.0) == 141
    assert over_render_frames(8.0) == 209
    for eighths in range(1, 15 * 8 + 1):
        duration = eighths / 8
        frames = over_render_frames(duration)
        assert (frames - 5) % 17 == 0, duration
        assert frames / H3_FPS > duration, duration
        assert frames >= (duration + OVER_RENDER_SECONDS) * H3_FPS - 0.5, duration


def test_the_lead_is_a_quarter_second_when_the_song_has_room():
    picture = over_render_frames(3.75) / H3_FPS
    lead = over_render_lead(
        start=12.0, duration=3.75, picture_seconds=picture, song_duration=154.6
    )
    assert lead == OVER_RENDER_LEAD_SECONDS == 0.25


def test_the_lead_never_reaches_before_the_song_starts():
    picture = over_render_frames(3.75) / H3_FPS
    assert over_render_lead(
        start=0.0, duration=3.75, picture_seconds=picture, song_duration=154.6
    ) == 0.0
    assert over_render_lead(
        start=0.1, duration=3.75, picture_seconds=picture, song_duration=154.6
    ) == 0.1


def test_the_lead_grows_to_keep_the_tail_inside_the_song():
    """A shot ending at the song's last second cannot extend its tail, so the whole margin
    shifts ahead of the window instead — the picture never shortens."""
    picture = over_render_frames(3.75) / H3_FPS  # 4.4583 s, extra 0.7083 s
    song = 100.0
    lead = over_render_lead(
        start=96.25, duration=3.75, picture_seconds=picture, song_duration=song
    )
    # The window ends exactly at the song's end: every bit of margin must lead.
    assert lead == pytest.approx(picture - 3.75)
    assert 96.25 - lead + picture == pytest.approx(song)
    # Part-way: the ideal quarter second plus exactly the overflow, no more. At 96.0 the
    # quarter-second lead leaves the tail 0.208 s past the song, so the lead grows to
    # 0.458 s and the window lands flush on the song's end.
    partial = over_render_lead(
        start=96.0, duration=3.75, picture_seconds=picture, song_duration=song
    )
    assert 96.0 - partial + picture == pytest.approx(song)
    assert OVER_RENDER_LEAD_SECONDS < partial < picture - 3.75
    # And a shot whose tail already fits keeps the ideal lead untouched.
    assert over_render_lead(
        start=95.0, duration=3.75, picture_seconds=picture, song_duration=song
    ) == OVER_RENDER_LEAD_SECONDS


def test_a_whole_song_shot_gets_no_lead_and_keeps_its_picture():
    """No room either side: the lead stays clamped to the start (0) and the caller clamps
    the trim's end at the song — the mismatch renders, exactly as pre-margin edge shots
    did, rather than being silently truncated or refused."""
    picture = over_render_frames(100.0) / H3_FPS
    assert over_render_lead(
        start=0.0, duration=100.0, picture_seconds=picture, song_duration=100.0
    ) == 0.0


def test_an_unknown_song_length_never_shifts_the_lead():
    """`song_duration == 0` means the length was never recorded; nothing can overflow an
    unknown, so the lead stays the ideal quarter second."""
    picture = over_render_frames(3.75) / H3_FPS
    assert over_render_lead(
        start=50.0, duration=3.75, picture_seconds=picture, song_duration=0.0
    ) == OVER_RENDER_LEAD_SECONDS


# --- Short windows: render the minimum, expose the window (Director, 2026-08-20) ----------
#
# "some may go long, some may be short … Shots still generated to the 4 second minimum and
# they just have a bigger invisible 'buffer' while still being lined up so the exposed part
# matches", and, the same day: "if a shot is below the minimum typical then the math would
# just center around that clip … spanning equally both directions."
#
# The two halves are tested separately because they can fail separately: the floor is about
# how much is rendered, the centring is about where the rendered part sits, and only the
# second one can move the exposed slice off its musical position.


def test_the_render_floor_holds_every_short_window_at_h3s_minimum():
    """A window under H3's trained minimum renders the minimum, not its own short length.

    The measured defect: a 2.083 s window asked for 73 frames — 3.042 s, under the 4 s H3 is
    trained for — because `over_render_frames` snapped the margin to the grid with no floor
    under it. The Director's five hand-timed windows are the live cases and they are named
    here as such.
    """
    assert H3_MIN_RENDER_FRAMES == 107
    assert H3_MIN_RENDER_FRAMES / H3_FPS >= H3_MIN_SHOT_SECONDS
    # The Director's own project (`project_59f14d19ff10`), edited to musical timing by hand.
    for duration in (2.083, 2.5, 2.667, 3.292, 3.708):
        assert over_render_frames(duration) == 107, duration
    # And every window from half a second up, not only those five.
    for eighths in range(4, 8 * 15 + 1):
        duration = eighths / 8
        assert over_render_frames(duration) / H3_FPS >= H3_MIN_SHOT_SECONDS, duration
        assert over_render_frames(duration) >= H3_MIN_RENDER_FRAMES, duration


def test_windows_h3_could_already_render_are_unchanged_frame_for_frame():
    """The pin. Only the short-window path may move; everything else renders what it always
    rendered, and the floor is asserted *not* to fire rather than merely to be harmless.

    The comparison is against the pre-floor arithmetic written out here — the expression this
    function carried before 2026-08-20 — because a pin against the current implementation
    would pin nothing at all.
    """
    def before_the_floor(duration: float) -> int:
        return align_h3_frames(max(5, round((duration + OVER_RENDER_SECONDS) * H3_FPS)))

    # The numbers the shipped tests above already pin, restated as the boundary they sit on.
    assert over_render_frames(3.75) == before_the_floor(3.75) == 107
    assert over_render_frames(4.0) == before_the_floor(4.0) == 124
    assert over_render_frames(5.0) == before_the_floor(5.0) == 141
    assert over_render_frames(8.0) == before_the_floor(8.0) == 209
    assert over_render_frames(15.0) == before_the_floor(15.0) == 379
    # 3.2917 s is the shortest window whose margin already clears the floor: (3.2917+0.5)·24
    # rounds to 91, which snaps to 107 on its own. From there to the H3 ceiling, nothing moved.
    for thousandths in range(3292, 15_001):
        duration = thousandths / 1000
        assert over_render_frames(duration) == before_the_floor(duration), duration
        assert not over_render_centred(duration), duration
    # Below it the two disagree, which is the whole change — and the disagreement is always
    # upward, never a shortened take.
    for thousandths in range(1, 3271):
        duration = thousandths / 1000
        assert over_render_frames(duration) > before_the_floor(duration), duration


def test_a_short_window_centres_its_take_on_the_window():
    """The Director's second ruling: the buffer spans "equally both directions".

    A 2.083 s window at 12 s renders 4.4583 s, and the take's midpoint is the window's
    midpoint to within the half frame the grid snap can shave — so the exposed cut sits in
    the middle of what H3 performed rather than at its head.
    """
    duration, start, song = 2.083, 12.0, 154.0
    picture = over_render_frames(duration) / H3_FPS
    lead = over_render_lead(
        start=start, duration=duration, picture_seconds=picture, song_duration=song
    )
    # 29 frames: half the 2.3753 s buffer is 28.504 frames, snapped to a whole one.
    assert lead == 29 / H3_FPS
    take_start, take_end = start - lead, start - lead + picture
    assert (take_start + take_end) / 2 == pytest.approx(
        start + duration / 2, abs=0.5 / H3_FPS
    )
    # Buffer on both sides, not merely somewhere: the take genuinely opens before the window
    # and closes after it.
    assert take_start < start and take_end > start + duration


def test_the_centred_lead_replaces_the_quarter_second_and_never_adds_to_it():
    """One lead, not two. The sync lead and the centring lead are the same number.

    `OVER_RENDER_LEAD_SECONDS` is the *ideal* for a margin-sized render; a centred window's
    ideal is half its buffer, and it stands in for the quarter second rather than stacking on
    it. It is never the smaller of the two either — the shortest buffer a centred window can
    have is 1.1875 s, so its half is 0.594 s — which is what makes the substitution safe: a
    short shot always gets *more* sync context than the quarter second, never less.
    """
    song = 154.0
    for duration in (0.5, 1.0, 2.0, 2.083, 2.5, 2.667, 3.25):
        picture = over_render_frames(duration) / H3_FPS
        extra = picture - duration
        lead = over_render_lead(
            start=60.0, duration=duration, picture_seconds=picture, song_duration=song
        )
        assert lead == round(extra / 2 * H3_FPS) / H3_FPS, duration
        assert lead > OVER_RENDER_LEAD_SECONDS, duration
        # Not the two added together, which is the arithmetic this test exists to refuse.
        assert lead != pytest.approx(OVER_RENDER_LEAD_SECONDS + extra / 2), duration
        # And never more than half the take, which is what adding them would eventually be.
        assert lead <= picture / 2, duration
    # The other side of the boundary keeps the quarter second exactly.
    for duration in (3.292, 3.75, 4.0, 12.0):
        picture = over_render_frames(duration) / H3_FPS
        assert over_render_lead(
            start=60.0, duration=duration, picture_seconds=picture, song_duration=song
        ) == OVER_RENDER_LEAD_SECONDS, duration


def test_the_centred_lead_is_a_whole_frame_so_the_cut_is_frame_exact():
    """Assembly cuts with `trim=start_frame=round(lead·24)` and the Monitor seeks the same
    frame, so a lead that is not a whole frame loses up to half a frame of alignment on every
    short shot. The centred ideal is snapped; the quarter second is already six frames."""
    for duration in (0.5, 1.0, 2.0, 2.083, 2.5, 2.667, 3.25):
        picture = over_render_frames(duration) / H3_FPS
        lead = over_render_lead(
            start=60.0, duration=duration, picture_seconds=picture, song_duration=154.0
        )
        assert lead * H3_FPS == round(lead * H3_FPS), duration


def test_a_short_window_at_the_songs_start_cannot_centre_and_keeps_its_exposure():
    """There is no song before 0 s to grab, so the whole buffer becomes tail — and the
    exposed slice does not move a frame for it."""
    duration, song = 2.083, 154.0
    picture = over_render_frames(duration) / H3_FPS
    lead = over_render_lead(
        start=0.0, duration=duration, picture_seconds=picture, song_duration=song
    )
    assert lead == 0.0
    # The take begins exactly at the window, and the exposed slice is still song 0-2.083.
    assert 0.0 - lead == 0.0
    assert (0.0 - lead) + lead == 0.0
    assert (0.0 - lead) + lead + duration == duration
    # Part-way in, the lead is whatever room the song actually has, not the ideal.
    partial = over_render_lead(
        start=0.4, duration=duration, picture_seconds=picture, song_duration=song
    )
    assert partial == 0.4 < 29 / H3_FPS


def test_a_short_window_at_the_songs_end_shifts_its_buffer_ahead_of_the_window():
    """The mirror of the 0 s case: the tail has nowhere to go, so it moves to the head.

    The picture is never shortened and the window never moves — the take simply ends on the
    song's last second, with every remaining frame of buffer in front of the cut.
    """
    duration, song = 2.083, 154.0
    picture = over_render_frames(duration) / H3_FPS
    start = song - duration
    lead = over_render_lead(
        start=start, duration=duration, picture_seconds=picture, song_duration=song
    )
    assert lead == pytest.approx(picture - duration)
    assert start - lead + picture == pytest.approx(song)
    # Every bit of buffer is now ahead of the window, and the window is still its own seconds.
    assert start - lead + lead == pytest.approx(start)


def test_no_clamp_ever_moves_a_short_windows_exposed_slice():
    """The property the whole feature stands on, swept over the song rather than sampled.

    For every start from 0 to the song's end, and for every short window length: the take
    never begins before the song, take second `lead` is the window's own start, the exposed
    slice is the window's own seconds, and the lead never exceeds the buffer there is. A clamp
    that dragged the exposure off its musical position would fail here whichever branch made
    it — the assertions do not know which branch ran.
    """
    song = 154.0
    for duration in (0.5, 2.083, 2.5, 3.25):
        picture = over_render_frames(duration) / H3_FPS
        extra = picture - duration
        for hundredths in range(0, int((song - duration) * 100) + 1, 37):
            start = hundredths / 100
            lead = over_render_lead(
                start=start, duration=duration, picture_seconds=picture, song_duration=song
            )
            take_start = start - lead
            assert 0.0 <= lead <= extra, (duration, start)
            assert take_start >= -1e-9, (duration, start)
            # The invariant: take second `t` is song second `take_start + t`, so the slice
            # assembly cuts at `lead` is the window, exactly, at every start.
            assert take_start + lead == pytest.approx(start), (duration, start)
            assert take_start + lead + duration == pytest.approx(start + duration), (
                duration, start
            )
            # The tail stays inside the song unless the song has no room at all to give.
            if lead < extra and lead < start:
                assert take_start + picture <= song + 1e-9, (duration, start)


def test_the_centred_boundary_is_where_the_floor_fires_and_nowhere_else():
    """`over_render_centred` is the one place the two 2026-08-20 rulings are reconciled, so
    the boundary is asserted rather than left to follow from whichever caller reads it.

    Centring is exactly "the floor invented this buffer". A window whose own margin already
    reaches H3's minimum keeps the shipped quarter-second rule — that band is 3.2917 s to 4 s,
    where "below the minimum typical" and "the floor fired" disagree.
    """
    for duration in (0.125, 1.0, 3.25, 3.2708333333333335):
        assert over_render_centred(duration), duration
        assert margin_frames(duration) < H3_MIN_RENDER_FRAMES, duration
        assert over_render_frames(duration) == H3_MIN_RENDER_FRAMES, duration
    for duration in (3.292, 3.5, 3.75, 3.99, 4.0, 15.0):
        assert not over_render_centred(duration), duration
        assert margin_frames(duration) >= H3_MIN_RENDER_FRAMES, duration
        assert over_render_frames(duration) == margin_frames(duration), duration


# --- Populate Timeline's tiling repair (spec-populate-timeline) ---------------------------


def test_populate_windows_tiles_the_whole_song_inside_h3s_range():
    """The model's layout is shape, not arithmetic: whatever it proposed, the result is
    contiguous from 0 to the song's end (within the millisecond the end-floor may shave),
    every window in 4-15 s — and the last window **never ends past the song**, because a
    window 0.1 ms over is a shot `song_audio_window` refuses whole. Found live: song
    154.644898 s, last window rounded to end at 154.645."""
    proposals = [(0.0, 2.0), (2.0, 30.0), (40.0, 7.0)]  # sloppy: gaps, out-of-range
    for song in (60.0, 154.644898, 33.333333):
        windows = populate_windows(proposals, song)
        assert windows[0][0] == 0.0
        cursor = 0.0
        for start, duration in windows:
            assert start == pytest.approx(cursor, abs=1e-6)
            assert 4.0 - 1e-9 <= duration <= 15.0 + 1e-9
            cursor = start + duration
        assert cursor == pytest.approx(song, abs=2e-3)
        assert cursor <= song + 1e-9, f"last window ends past the song at {song}"


def test_populate_windows_output_is_pinned_value_for_value():
    """The repair itself, frozen. The route above it now *enforces* how many shots the
    model must return, and the temptation that comes with count enforcement is to let the
    enforced count leak down here and start deciding geometry. It must not: this function
    is the arithmetic, the model's numbers are only shape, and both lists below are the
    bytes it produced before count enforcement existed. A change to either is a change to
    the tiling contract and has to be argued for, not absorbed."""
    assert populate_windows([(0.0, 2.0), (2.0, 30.0), (40.0, 7.0)], 154.644898) == [
        (0.0, 8.249),
        (8.249, 15.0),
        (23.249, 11.396),
        (34.645, 15.0),
        (49.645, 15.0),
        (64.645, 15.0),
        (79.645, 15.0),
        (94.645, 15.0),
        (109.645, 15.0),
        (124.645, 15.0),
        (139.645, 14.999),
    ]
    # And populate's own call shape: the enforced 6 s ceiling, twelve proposals for 60 s.
    assert populate_windows(
        [(index * 5.0, 5.0) for index in range(12)], 60.0, maximum=6.0
    ) == [(index * 5.0, 5.0) for index in range(12)]


def test_populate_windows_clamps_the_count_to_the_feasible_band():
    # 154.6 s: at most 15 s per shot means at least 11 shots, however few were proposed.
    few = populate_windows([(0, 50), (50, 50), (100, 54.6)], 154.6)
    assert len(few) == 11
    # 60 s: at least 4 s per shot means at most 15, however many were proposed.
    many = populate_windows([(i * 1.5, 1.5) for i in range(40)], 60.0)
    assert len(many) == 15
    # And a comfortable proposal count survives as-is.
    six = populate_windows([(i * 10, 10.0) for i in range(6)], 60.0)
    assert len(six) == 6


def test_populate_windows_preserves_the_proposals_relative_shape():
    windows = populate_windows([(0.0, 5.0), (5.0, 10.0)], 24.0)
    assert len(windows) == 2
    assert windows[0][1] < windows[1][1]
    assert windows[0][1] + windows[1][1] == pytest.approx(24.0)


def test_populate_windows_handles_the_tiny_song_and_refuses_the_impossible_one():
    assert populate_windows([], 3.0) == [(0.0, 3.0)]
    with pytest.raises(TimelineError):
        populate_windows([], 0.0)
    # No proposals at all still tiles: the default count aims at ~9.5 s windows.
    bare = populate_windows([], 60.0)
    assert sum(duration for _, duration in bare) == pytest.approx(60.0)


def test_proposal_for_position_maps_by_proportional_span():
    assert proposal_for_position(5.0, 30.0, 3) == 0
    assert proposal_for_position(15.0, 30.0, 3) == 1
    assert proposal_for_position(29.9, 30.0, 3) == 2
    assert proposal_for_position(30.0, 30.0, 3) == 2  # clamped at the end
    with pytest.raises(TimelineError):
        proposal_for_position(1.0, 30.0, 0)


# --- Sections: the Director's marks, and the lyric-block pairing (2026-08-19) --------------


def sectioned_project() -> Project:
    project = Project(name="Sections")
    project.song = Song(
        title="S", source="imported", duration=60,
        lyrics=(
            "[Verse]\nline one\nline two\n\n[Chorus]\nhook line\n\n"
            "[Verse]\nsecond verse words\n\n[Outro]\nfade out\n"
        ),
    )
    project.sections = [
        SongSection(label="Intro", start=0, duration=8),
        SongSection(label="Verse 1", start=8, duration=16, prompt="at the standing mic"),
        SongSection(label="Chorus", start=24, duration=12, prompt="on the canopy bed"),
        SongSection(label="Verse 2", start=36, duration=12),
        SongSection(label="Outro", start=48, duration=12),
    ]
    return project


def test_song_section_maps_by_midpoint_and_absence_means_unknown():
    project = sectioned_project()
    assert song_section(project, Shot(start=30, duration=6, prompt="x")).label == "Chorus"
    # A shot straddling a boundary belongs to whichever section owns its midpoint.
    assert song_section(project, Shot(start=22, duration=6, prompt="x")).label == "Chorus"
    assert song_section(project, Shot(start=20, duration=6, prompt="x")).label == "Verse 1"
    # No sections marked: None, never a fabricated boundary.
    bare = Project(name="Bare")
    assert song_section(bare, Shot(start=1, duration=4, prompt="x")) is None


def test_section_lyrics_pair_by_order_of_appearance_within_a_label_family():
    """The sheet's tags carry structure but no timing; the sections carry timing but no
    words; the Nth "Verse *" section takes the Nth [Verse] block. This is the fix for the
    wrong-verse lipsync the first batch rendered: a chorus shot at 30 s was expanded with
    the song's opening line."""
    project = sectioned_project()
    verse1, chorus, verse2, outro = project.sections[1:]
    assert section_lyrics(project, verse1) == "line one\nline two"
    assert section_lyrics(project, chorus) == "hook line"
    assert section_lyrics(project, verse2) == "second verse words"
    assert section_lyrics(project, outro) == "fade out"
    # A section with no matching block answers "" — no words, never a guess.
    assert section_lyrics(project, project.sections[0]) == ""
    assert section_lyrics(project, None) == ""


def test_the_expansion_payload_carries_the_shots_section_block():
    project = sectioned_project()
    project.shots = [Shot(id="shot_c", start=27, duration=6, prompt="Glamour angle")]
    built = shot_expansion_input(project, project.shots[0])
    section = built["shot"]["section"]
    assert section["label"] == "Chorus"
    assert section["prompt"] == "on the canopy bed"
    # No lyric text, deliberately: `section_lyrics` remains a planning surface, but the
    # expansion never sees words (2026-08-19, twice-measured — see the payload builder).
    assert "lyrics" not in section
    assert "hook line" not in json.dumps(built)
    # 27s into a 24-36s section: a quarter of the way through.
    assert section["clip_position"] == 0.25
    # Sectionless project: the key is absent, never empty — absence must not read as a
    # confident claim.
    bare = Project(name="Bare", song=project.song)
    bare.shots = [Shot(id="shot_c", start=27, duration=6, prompt="Glamour angle")]
    assert "section" not in shot_expansion_input(bare, bare.shots[0])["shot"]


def test_lyric_blocks_align_to_transcribed_words_and_refrains_stay_home():
    """The sheet is the truth about the words, the transcript about the clock — and a
    repeated refrain must land on ITS repeat, not a later one. Modeled on the live song:
    Whisper normalizes contractions ("runnin" -> "running"), mishears a word mid-line
    ("lap it up" -> "light me up"), and the chorus recurs verbatim in the outro."""
    from music_video_producer.timeline import align_lyric_blocks

    sheet = (
        "[Verse]\nI keep runnin' through the night\nchasing every fadin' light\n"
        "[Chorus]\nlick it hard lap it up right now\ncome do that wicked deed\n"
        "[Outro]\nlick it hard lap it up right now\ncome do that wicked deed\n"
    )
    words = []
    clock = 10.0
    for text in ["I", "keep", "running", "through", "the", "night", "chasing", "every", "fading", "light"]:
        words.append((text, clock, clock + 0.4)); clock += 0.5
    clock = 30.0  # instrumental gap
    for text in ["lick", "it", "hard", "light", "me", "up", "right", "now", "come", "do", "that", "wicked", "deed"]:
        words.append((text, clock, clock + 0.4)); clock += 0.5
    clock = 60.0  # long instrumental bridge, then the identical outro refrain
    for text in ["lick", "it", "hard", "lap", "it", "up", "right", "now", "come", "do", "that", "wicked", "deed"]:
        words.append((text, clock, clock + 0.4)); clock += 0.5

    aligned = align_lyric_blocks(sheet, words)
    assert [tag for tag, *_ in aligned] == ["Verse", "Chorus", "Outro"]
    verse, chorus, outro = aligned
    assert verse[1] == 10.0 and 13.5 <= verse[2] <= 15.0
    # The chorus stayed on its own (misheard) repeat rather than jumping to the outro's
    # word-perfect one 30 seconds later.
    assert chorus[1] == 30.0 and chorus[2] < 40.0
    assert outro[1] == 60.0
    # A sheet block the track never sings is omitted, not guessed.
    unsung = align_lyric_blocks(sheet + "[Bridge]\nwords nobody ever sang here\n", words)
    assert [tag for tag, *_ in unsung] == ["Verse", "Chorus", "Outro"]


def test_aligned_blocks_tile_the_whole_song_as_sections():
    """Intro when the voice starts late, ordinals on repeats, instrumental tails belonging
    to the section they follow, the last section running out the song."""
    from music_video_producer.timeline import proposed_sections_from_alignment

    proposals = proposed_sections_from_alignment(
        [("Verse", 11.0, 31.2), ("Chorus", 32.5, 50.9), ("Verse", 56.4, 77.2)],
        duration := 120.0,
    )
    assert proposals == [
        ("Intro", 0.0, 11.0, ""),
        ("Verse", 11.0, 21.5, ""),
        ("Chorus", 32.5, 23.9, ""),
        ("Verse 2", 56.4, 63.6, ""),
    ]
    assert proposals[-1][1] + proposals[-1][2] == duration
    # A voice inside the first two seconds absorbs the opening into its own section.
    early = proposed_sections_from_alignment([("Verse", 0.8, 20.0)], 60.0)
    assert early == [("Verse", 0.0, 60.0, "")]
    assert proposed_sections_from_alignment([], 60.0) == []


def test_repair_sections_sorts_clamps_and_truncates_overlaps():
    """Model-proposed structure made legal without refusal: a proposal is scaffolding the
    Director will drag, so repair beats a 502. Gaps survive — unmarked means unknown."""
    repaired = repair_sections(
        [
            ("Chorus", 30.0, 40.0, "on the bed"),     # overlaps the next; truncated
            ("Verse", 8.0, 22.0, "at the mic"),       # arrives out of order
            ("Outro", 58.0, 30.0, ""),                # runs past the song; clamped
            ("Blip", 59.0, 0.4, ""),                  # sub-second after clamping; dropped
            ("Ghost", 70.0, 10.0, ""),                # starts past the song; dropped
        ],
        60.0,
    )
    assert [entry[0] for entry in repaired] == ["Verse", "Chorus", "Outro"]
    verse, chorus, outro = repaired
    assert verse == ("Verse", 8.0, 22.0, "at the mic")
    # Chorus truncated at Outro's start; Outro clamped to the song's end.
    assert chorus == ("Chorus", 30.0, 28.0, "on the bed")
    assert outro == ("Outro", 58.0, 2.0, "")


# --- The appearance anchor (`Asset.consistency_prompt`) ------------------------------------


def _anchored(**fields) -> Asset:
    return Asset(name=fields.pop("name", "Lucy"), kind="character", path="a.png", **fields)


def test_an_asset_with_no_anchor_composes_the_bare_label_unchanged():
    """The identity every consumer depends on, asserted on the composition itself.

    `anchored_label` is the one place the anchor is joined to a name, so this is where the
    empty case has to be exactly the input string — not equal-after-strip, not equal-modulo-a
    -trailing-comma. Whitespace-only counts as no anchor, because that is what a Director who
    cleared the box meant and because a single space would otherwise put a dangling comma
    into every tag line citing the asset.
    """
    for stored in ("", "   ", "\n\t "):
        asset = _anchored(consistency_prompt=stored)
        assert asset_anchor(asset) == ""
        assert anchored_label(asset, "Lucy") == "Lucy"
        assert anchored_label(asset, "the woman upstage") == "the woman upstage"

    anchored = _anchored(consistency_prompt="  a woman in a red\n  leather jacket ")
    # Collapsed, because the anchor travels inside one-line prompt sentences and a line break
    # reads as a shot boundary to the H3 specialist.
    assert asset_anchor(anchored) == "a woman in a red leather jacket"
    assert anchored_label(anchored, "Lucy") == "Lucy, a woman in a red leather jacket"


def test_the_anchor_wins_over_the_generation_prompt_and_the_vision_summary():
    """The precedence the whole field exists for, on the one function that decides it.

    Three assets carrying the three sources in every combination that matters. The anchor is
    the Director's assertion and outranks both machine-written descriptions; without one the
    vision summary still beats the generation prompt, which is the ordering this function had
    before the anchor existed and which must not have moved.
    """
    vision = VisionInspectionRecord(summary="a woman in a green parka")

    both = _anchored(
        prompt="a woman in a blue dress, studio lighting",
        vision=vision,
        consistency_prompt="a woman in a red leather jacket",
    )
    assert _asset_description(both) == "a woman in a red leather jacket"

    inspected = _anchored(prompt="a woman in a blue dress", vision=vision)
    assert _asset_description(inspected) == "a woman in a green parka"

    generated = _anchored(prompt="a woman in a blue dress")
    assert _asset_description(generated) == "a woman in a blue dress"

    assert _asset_description(_anchored()) == ""

    # And it wins where it is actually consumed, not only in the helper.
    project = Project(
        name="Library",
        assets=[both],
        shots=[Shot(id="s1", start=0.0, duration=4.0, prompt="A street at dawn")],
    )
    library = assistant_input(project, shot_ids=["s1"])["assets"]
    assert library[0]["description"] == "a woman in a red leather jacket"


def test_a_held_but_anchorless_asset_leaves_the_expansion_input_byte_identical():
    """The pinned digests above are computed over a project holding no assets at all.

    That is the shape those two shots were written in, and it means they cannot see a
    regression in the new lookup: a builder that appended an anchor key unconditionally, or
    that emitted `"anchor": ""`, would pass them and change every real project's payload. So
    the same two shots are digested again with their cited assets actually present and
    anchor-free, against the same two numbers.
    """
    project = Project(
        id="project_pinned0001", name="Pinned",
        creative_brief="brief", treatment="treatment", style_bible="style",
        song=Song(title="Harder Faster", source="imported", duration=154.6,
                  lyrics="[verse] there is a hunger", caption="mid-tempo metal"),
        assets=[
            Asset(id=asset_id, name=asset_id, kind="character", path=f"{asset_id}.png")
            for asset_id in ("asset_a", "asset_b", "asset_1", "asset_2")
        ],
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


def test_the_expansion_input_names_each_anchored_reference_beside_its_tag():
    """The anchor arrives numbered, so `<Picture 2>` and the phrase name one subject.

    Composed by `anchored_label` — the same composition the reference map uses, including a
    per-shot rename — because the specialist writing "<Picture 2>" and the render conditioning
    that slot must be told about the same person in the same words. A citation whose asset the
    project does not hold gets no key at all: nothing is known about it, and the render refuses
    it by name rather than being pre-empted here.
    """
    project = Project(
        name="Anchored",
        assets=[
            Asset(id="asset_lucy", name="Lucy", kind="character", path="lucy.png",
                  consistency_prompt="a woman in a red leather jacket"),
            Asset(id="asset_dock", name="Dock", kind="setting", path="dock.png"),
        ],
        shots=[
            Shot(id="s1", start=0.0, duration=4.0, prompt="She steps off the kerb",
                 mode="references",
                 reference_labels={"asset_lucy": "the woman upstage"},
                 citations=[
                     AssetCitation(asset_id="asset_dock", role="reference", order=0),
                     AssetCitation(asset_id="asset_lucy", role="reference", order=1),
                     AssetCitation(asset_id="asset_gone", role="reference", order=2),
                 ]),
        ],
    )

    references = shot_expansion_input(project, project.shots[0])["shot"]["references"]

    assert [reference["tag"] for reference in references] == [
        "<Picture 1>", "<Picture 2>", "<Picture 3>"
    ]
    # The anchorless setting and the dangling citation both carry no key, rather than an
    # empty one: absent means "nothing is stored", and `""` would be a claim.
    assert "anchor" not in references[0]
    assert "anchor" not in references[2]
    assert references[1]["anchor"] == "the woman upstage, a woman in a red leather jacket"


# ------------------------------------------------------------------------------------------
# Snapping cuts to phrase boundaries. Everything below is pure: no client, no store, no disk.
# ------------------------------------------------------------------------------------------


def snap_song(spans, duration=20.0):
    """A Song that has been heard — the measurement `snap_cut_plan` places cuts against.

    `lyric_words` is the raw transcript and `vocal_spans` is `merge_vocal_spans`' output over
    it, so the two describe the **same** voice: one word filling each span. That consistency
    is what a real `align-lyrics` write produces (`app.py` writes the words and then merges
    exactly those words), and since `vocal_gaps` reads the words first, a fixture whose words
    contradicted its spans would be measuring a track that cannot exist. The word-level
    finding has its own fixture below, where the two deliberately differ — as they do on a
    real song, because the merge bridges every rest under 0.75 s.
    """
    return Song(
        title="Measured",
        path="measured.flac",
        source="imported",
        duration=duration,
        lyric_words=[("word", span_start, span_end) for span_start, span_end in spans],
        vocal_spans=spans,
    )


def tiled(project):
    """Every adjacency in a project's shots, as (previous end, next start) pairs."""
    ordered = ordered_shots(project)
    return [(a.end, b.start) for a, b in pairwise(ordered)]


def test_the_snap_proposal_reads_the_project_and_writes_nothing_to_it():
    """Pure and I/O-free: the manifest is byte-identical after a report.

    The whole report-then-confirm shape rests on this — a proposal that mutated the Project it
    was handed would make "nothing was written" a claim about the route rather than a fact
    about the function, and the route re-reads nothing between the report and the apply.
    """
    project = Project(
        name="Untouched",
        song=snap_song([(0.5, 7.0), (8.0, 13.0), (14.0, 19.5)], duration=30.0),
        shots=[Shot(id=f"s{index}", start=index * 6.0, duration=6.0) for index in range(5)],
    )
    before = project.model_dump(mode="json")

    plan = snap_cut_plan(project, tolerance=1.5)

    assert plan.moves, "this fixture is meant to produce moves, or it asserts nothing"
    assert project.model_dump(mode="json") == before
    # The proposal's own windows disagree with the project, which is the point: they are a
    # proposal. Nothing has been applied to the shots.
    assert [(shot.start, shot.duration) for shot in project.shots] == [
        (index * 6.0, 6.0) for index in range(5)
    ]


def test_a_cut_inside_a_phrase_moves_to_the_gap_and_one_without_a_gap_stays():
    """The feature itself, and its refusal to reach: two cuts, one of each, in one plan.

    The 6.0 s cut sits inside the phrase that runs 0.5–7.0 s, and the rest that follows it
    (7.0–8.0 s) is within tolerance, so it moves to `SNAP_CLEARANCE_SECONDS` inside that rest.
    The 18.0 s cut sits inside 14.0–19.5 s with its nearest rest 1.65 s away, past the 1.5 s
    tolerance, so it stays exactly where it was and says why.
    """
    project = Project(
        name="One of each",
        song=snap_song([(0.5, 7.0), (8.0, 13.0), (14.0, 19.5), (21.0, 26.0)], duration=30.0),
        shots=[Shot(id=f"s{index}", start=index * 6.0, duration=6.0) for index in range(5)],
    )

    plan = snap_cut_plan(project, tolerance=1.5)

    assert plan.status == "ready"
    assert {(move.boundary, move.proposed) for move in plan.moves} == {
        (6.0, round(7.0 + SNAP_CLEARANCE_SECONDS, 3)),
        (12.0, round(13.0 + SNAP_CLEARANCE_SECONDS, 3)),
    }
    stuck = [skip for skip in plan.skips if skip.boundary == 18.0]
    assert len(stuck) == 1
    assert stuck[0].reason == SNAP_NO_GAP_IN_TOLERANCE.format(
        before=stuck[0].before_label,
        after=stuck[0].after_label,
        boundary=18.0,
        tolerance=1.5,
    )
    # The cut that stayed left both its shots exactly where the plan had them.
    windows = {shot_id: (start, duration) for shot_id, start, duration in plan.windows}
    assert windows["s3"][0] == 18.0
    assert windows["s4"] == (24.0, 6.0)


def test_tolerance_zero_is_a_genuine_no_op():
    """Off means nothing was examined — not a loop that happened to find no candidates.

    Asserted the only way that distinguishes the two: the plan handed in is one
    `snap_cut_plan` would otherwise *raise* on (a gap between two shots means they share no
    cut). Tolerance 0 returns the windows untouched instead, because the switched-off branch
    is taken before the song, the shape of the plan or anything else is read.
    """
    project = Project(
        name="Off",
        song=snap_song([(0.5, 7.0), (8.0, 13.0)], duration=30.0),
        shots=[
            Shot(id="s0", start=0.0, duration=6.0),
            # A hole the contiguity check refuses — reached only if tolerance 0 does not
            # short-circuit.
            Shot(id="s1", start=9.0, duration=6.0),
        ],
    )

    plan = snap_cut_plan(project, tolerance=0)

    assert plan.status == "off"
    assert plan.message == SNAP_TOLERANCE_OFF
    assert plan.moves == []
    assert plan.skips == []
    assert plan.windows == [("s0", 0.0, 6.0), ("s1", 9.0, 6.0)]
    # And the same plan at a real tolerance does raise, which is what says the assertion above
    # is about the short circuit rather than about a fixture that happens to be quiet.
    with pytest.raises(TimelineError):
        snap_cut_plan(project, tolerance=SNAP_TOLERANCE_DEFAULT)


def test_a_move_that_would_shrink_a_neighbour_under_the_minimum_is_refused_for_that_cut():
    """The band refusal, named with the length and the bound, and the neighbours untouched.

    The 4.100 s cut's nearest rest is 3.5–3.8 s, which would leave the shot before it at
    3.650 s — under H3's 4 s floor. Refused for that cut alone: the plan's other cut is
    judged on its own and reports that it already lands clear.
    """
    project = Project(
        name="Too short",
        song=snap_song([(0.2, 3.5), (3.8, 11.0), (12.5, 18.0)], duration=20.0),
        shots=[
            Shot(id="s0", start=0.0, duration=4.1),
            Shot(id="s1", start=4.1, duration=7.9),
            Shot(id="s2", start=12.0, duration=8.0),
        ],
    )

    plan = snap_cut_plan(project, tolerance=1.0)

    assert plan.moves == []
    refused = next(skip for skip in plan.skips if skip.boundary == 4.1)
    assert refused.reason == SNAP_OUT_OF_BAND.format(
        before=refused.before_label,
        after=refused.after_label,
        boundary=4.1,
        proposed=3.65,
        neighbour=refused.before_label,
        length=3.65,
        bound="under",
        limit=H3_MIN_SHOT_SECONDS,
        edge="minimum",
    )
    assert "under the 4s minimum" in refused.reason
    # Neither neighbour moved, and neither did the cut beyond them.
    assert plan.windows == [("s0", 0.0, 4.1), ("s1", 4.1, 7.9), ("s2", 12.0, 8.0)]
    assert any(
        skip.reason
        == SNAP_ALREADY_SILENT.format(
            before=skip.before_label, after=skip.after_label, boundary=12.0
        )
        for skip in plan.skips
    )


def test_a_move_that_would_stretch_a_neighbour_past_the_maximum_is_refused_for_that_cut():
    """The other end of the same band. 14.800 s would snap to 15.350 s, past H3's 15 s ceiling."""
    project = Project(
        name="Too long",
        song=snap_song([(0.5, 15.2), (16.0, 19.0)], duration=20.0),
        shots=[
            Shot(id="s0", start=0.0, duration=14.8),
            Shot(id="s1", start=14.8, duration=5.2),
        ],
    )

    plan = snap_cut_plan(project, tolerance=1.0)

    assert plan.moves == []
    refused = plan.skips[0]
    assert refused.reason == SNAP_OUT_OF_BAND.format(
        before=refused.before_label,
        after=refused.after_label,
        boundary=14.8,
        proposed=15.35,
        neighbour=refused.before_label,
        length=15.35,
        bound="over",
        limit=H3_MAX_SHOT_SECONDS,
        edge="maximum",
    )
    assert "over the 15s maximum" in refused.reason
    assert plan.windows == [("s0", 0.0, 14.8), ("s1", 14.8, 5.2)]


def protected_plan(**shot_fields):
    """One three-shot plan whose middle shot carries whatever protection is being tested.

    Both cuts belong to the middle shot, so a protection on it must stop both — which is the
    geometric fact this feature turns on: a cut is shared, so either shot's protection is the
    cut's protection.
    """
    return Project(
        name="Protected",
        song=snap_song([(0.5, 7.0), (8.0, 13.0), (14.0, 19.5)], duration=24.0),
        shots=[
            Shot(id="s0", start=0.0, duration=6.0),
            Shot(id="s1", start=6.0, duration=6.0, **shot_fields),
            Shot(id="s2", start=12.0, duration=12.0),
        ],
    )


def test_a_locked_shot_never_has_a_cut_moved_under_it():
    project = protected_plan(locked=True)

    plan = snap_cut_plan(project, tolerance=1.5)

    assert plan.moves == []
    assert [skip.reason for skip in plan.skips] == [
        SNAP_LOCKED_REFUSAL.format(shot="SHOT 02 (s1)")
    ] * 2
    assert plan.windows == [("s0", 0.0, 6.0), ("s1", 6.0, 6.0), ("s2", 12.0, 12.0)]


def test_an_approved_shot_never_has_a_cut_moved_under_it():
    """AD-13, refused rather than auto-un-approved.

    Approval snapshots the window and assembly refuses a shot whose window moved afterwards,
    so a silent nudge here would trade a mouth mismatch for a plan that no longer assembles.
    The refusal says so and says what to do — un-approve — which is a decision about a take
    and is therefore the Director's, exactly as `POPULATE_PROTECTED_REFUSAL` rules.
    """
    project = protected_plan(
        status="approved",
        approved_output="takes/s1.mp4",
        approved_start=6.0,
        approved_duration=6.0,
    )

    plan = snap_cut_plan(project, tolerance=1.5)

    assert plan.moves == []
    assert [skip.reason for skip in plan.skips] == [
        SNAP_APPROVED_REFUSAL.format(shot="SHOT 02 (s1)")
    ] * 2
    # The snapshot still describes the live window, which is what keeps assembly quiet.
    approved = project.shots[1]
    assert (approved.approved_start, approved.approved_duration) == (
        approved.start,
        approved.duration,
    )


def test_a_shot_with_a_render_in_flight_never_has_a_cut_moved_under_it():
    """The set is passed in because the evidence is the job records, and `shot_render_in_flight`
    is the one reader of them. This asserts the plan honours it; `test_api` asserts the route
    builds it from the real jobs."""
    project = protected_plan()

    plan = snap_cut_plan(project, tolerance=1.5, rendering=frozenset({"s1"}))

    assert plan.moves == []
    assert [skip.reason for skip in plan.skips] == [
        SNAP_IN_FLIGHT_REFUSAL.format(shot="SHOT 02 (s1)")
    ] * 2


def test_a_lock_outranks_an_approval_and_an_approval_outranks_a_render():
    """One precedence, `shot_write_refusal`'s and `populate`'s: a lock is a decision the
    Director made, so when several apply it is the sentence worth reading."""
    both = protected_plan(locked=True, status="approved", approved_output="takes/s1.mp4")
    approved_and_rendering = protected_plan(status="approved", approved_output="t.mp4")

    assert snap_cut_plan(both, tolerance=1.5).skips[0].reason == SNAP_LOCKED_REFUSAL.format(
        shot="SHOT 02 (s1)"
    )
    assert snap_cut_plan(
        approved_and_rendering, tolerance=1.5, rendering=frozenset({"s1"})
    ).skips[0].reason == SNAP_APPROVED_REFUSAL.format(shot="SHOT 02 (s1)")


def test_a_song_with_no_alignment_is_an_explicitly_empty_branch():
    """`Song.vocal_spans` empty is **unmeasured, not silent** — `shot_vocal_overlap`'s rule.

    The alternative is the fabrication this codebase keeps catching: treating an
    untranscribed track as one long silence would place every cut in the plan against a
    silence nobody heard, and every one of them would be reported as a confident move.
    """
    project = Project(
        name="Unheard",
        song=snap_song([], duration=30.0),
        shots=[Shot(id=f"s{index}", start=index * 6.0, duration=6.0) for index in range(5)],
    )

    plan = snap_cut_plan(project, tolerance=1.5)

    assert plan.status == "unmeasured"
    assert plan.message == SNAP_UNMEASURED
    assert plan.moves == []
    assert plan.skips == []
    assert plan.windows == [(f"s{index}", index * 6.0, 6.0) for index in range(5)]
    # And the same is true with no Song at all, rather than raising on the attribute.
    assert (
        snap_cut_plan(Project(name="No song", shots=list(project.shots)), tolerance=1.5).status
        == "unmeasured"
    )


def test_vocal_gaps_answers_none_when_nothing_was_measured_and_the_complement_otherwise():
    song = snap_song([(2.0, 4.0), (3.5, 6.0), (9.0, 10.0)], duration=12.0)

    assert vocal_gaps(None, start=0.0, end=12.0) is None
    assert vocal_gaps(snap_song([], duration=12.0), start=0.0, end=12.0) is None
    # Overlapping spans merge rather than producing a negative-length gap, and the window
    # clamps both ends.
    assert vocal_gaps(song, start=0.0, end=12.0) == [(0.0, 2.0), (6.0, 9.0), (10.0, 12.0)]
    assert vocal_gaps(song, start=3.0, end=9.5) == [(6.0, 9.0)]


def measured_song(*, words=(), spans=(), duration=12.0):
    """A Song carrying exactly the measurements named — either, both or neither."""
    return Song(
        title="Measured",
        path="measured.flac",
        source="imported",
        duration=duration,
        lyric_words=list(words),
        vocal_spans=list(spans),
    )


def test_vocal_gaps_reads_the_words_first_and_the_merged_spans_only_as_a_fallback():
    """All four presence combinations of the two measurements, and what each answers.

    `Song.lyric_words` and `Song.vocal_spans` are independent fields — an older manifest can
    hold spans without words — so every combination is a real state and each is asserted here.

    * **Neither** is `None`: unmeasured, not silent. Two empty lists are two absences.
    * **Spans only** is the answer this function has always given, unchanged.
    * **Words present** wins, and the win is visible: the 0.6 s breath between "b" and "c" is
      a gap in the words and *not* a gap in the spans, because `merge_vocal_spans` bridges
      every rest under 0.75 s so the timeline's vocal band does not flicker. That threshold is
      a display decision, and this is the one place it was quietly deciding where cuts go.
    * **Both** answers exactly what words-only answers, so the spans cannot dilute them.
    """
    words = [("a", 2.0, 4.0), ("b", 4.6, 6.0), ("c", 9.0, 10.0)]
    spans = merge_vocal_spans(words)
    assert spans == [(2.0, 6.0), (9.0, 10.0)], "the merge is what hides the 0.6 s rest"

    assert vocal_gaps(measured_song(), start=0.0, end=12.0) is None
    assert vocal_gaps(measured_song(spans=spans), start=0.0, end=12.0) == [
        (0.0, 2.0), (6.0, 9.0), (10.0, 12.0)
    ]
    from_words = [(0.0, 2.0), (4.0, 4.6), (6.0, 9.0), (10.0, 12.0)]
    assert vocal_gaps(measured_song(words=words), start=0.0, end=12.0) == from_words
    assert vocal_gaps(measured_song(words=words, spans=spans), start=0.0, end=12.0) == from_words
    # The window still clamps both ends on the word path, and a gap crossing an edge is cut
    # at the edge rather than reported whole.
    assert vocal_gaps(measured_song(words=words), start=4.2, end=8.0) == [(4.2, 4.6), (6.0, 8.0)]


def test_neither_measurement_being_empty_is_ever_read_as_silence():
    """The rule that must survive the change, stated over each empty list separately.

    "Unmeasured is not silent" is what stops a plan being placed against a silence nobody
    heard, and there are now two lists that can be empty. Empty words with real spans falls
    through to the spans; empty spans with real words reads the words; both empty is `None`.
    The failure this guards is the easy one — a source-selection that treats an empty
    `lyric_words` as "no voice anywhere" and answers that the whole song is a gap.
    """
    words = [("a", 2.0, 4.0)]
    spans = [(2.0, 4.0)]

    heard = [(0.0, 2.0), (4.0, 6.0)]
    assert vocal_gaps(measured_song(words=words, spans=[]), start=0.0, end=6.0) == heard
    assert vocal_gaps(measured_song(words=[], spans=spans), start=0.0, end=6.0) == heard
    assert vocal_gaps(measured_song(words=[], spans=[]), start=0.0, end=6.0) is None
    assert vocal_gaps(None, start=0.0, end=6.0) is None
    # And the whole window is never the answer for an unmeasured track, which is the concrete
    # harm the rule exists to prevent.
    for song in (measured_song(), None):
        assert vocal_gaps(song, start=0.0, end=6.0) != [(0.0, 6.0)]


# The Director's own track, measured 2026-08-20: "Harder Faster (Female Cover)", 154.6 s and
# 262 aligned words. These are its real voiceless stretches — the intro, the outro and the
# seven interior rests — read out of `project_59f14d19ff10`'s alignment.
REAL_SONG_SECONDS = 154.64
REAL_VOICELESS = [
    (0.0, 11.0),        # intro
    (44.06, 44.80),     # 0.74 — under the merge threshold, invisible to spans
    (87.72, 89.14),     # 1.42
    (89.66, 90.32),     # 0.66 — under the merge threshold
    (99.20, 103.20),    # 4.00
    (108.02, 108.66),   # 0.64 — under the merge threshold
    (120.10, 123.20),   # 3.10
    (125.00, 125.72),   # 0.72 — under the merge threshold
    (134.56, REAL_SONG_SECONDS),  # outro
]


def real_song_words():
    """A word list whose voiceless complement is `REAL_VOICELESS`, sung in half-second words.

    The words *inside* a phrase are 0.04 s apart, which is what a real transcript looks like
    and what makes this fixture worth its length: those breaths are far under
    `SNAP_MINIMUM_GAP_SECONDS`, so the word path must drop them, and they are what a naive
    complement-of-the-words would offer a cut instead of the real rest seconds away.
    """
    words: list[tuple[str, float, float]] = []
    sung: list[tuple[float, float]] = []
    cursor = 0.0
    for low, high in REAL_VOICELESS:
        if low > cursor:
            sung.append((cursor, low))
        cursor = high
    if REAL_SONG_SECONDS > cursor:
        sung.append((cursor, REAL_SONG_SECONDS))
    for start, end in sung:
        at = start
        while at < end - 1e-9:
            stop = min(round(at + 0.5, 2), end)
            words.append(("word", round(at, 2), round(stop, 2)))
            at = round(stop + 0.04, 2)
        # The phrase ends where the measurement says it ends: the last word is stretched to it
        # rather than leaving a breath-width sliver the gap boundaries would then be wrong by.
        words[-1] = (words[-1][0], words[-1][1], end)
    return words


def test_the_word_level_gaps_are_the_ones_the_merged_spans_hide():
    """The measurement this change exists for, as a falsifiable assertion. **3 versus 7.**

    Seeded with the Director's own song's real gap boundaries, and the spans derived by the
    **real `merge_vocal_spans`** rather than written out by hand, so the fixture cannot drift
    from what the application actually stores. The words leave seven interior rests of 0.6 s
    or more; the merge bridges four of them — every one under its 0.75 s threshold — and
    leaves three. `SNAP_CLEARANCE_SECONDS` is 0.15 s at both ends, so a cut needs about 0.3 s
    of room and the smallest of the four (0.64 s) clears it twice over.

    Without this test, "simplify `vocal_gaps` back to the spans" halves what snapping can see
    and every existing test still passes.
    """
    words = real_song_words()
    spans = merge_vocal_spans(words)
    def interior(gaps):
        """The gaps that are rests *between* phrases — the intro and outro dropped."""
        return [(low, high) for low, high in gaps if low > 0 and high < REAL_SONG_SECONDS]

    from_words = vocal_gaps(
        measured_song(words=words, duration=REAL_SONG_SECONDS),
        start=0.0,
        end=REAL_SONG_SECONDS,
    )
    from_spans = vocal_gaps(
        measured_song(spans=spans, duration=REAL_SONG_SECONDS),
        start=0.0,
        end=REAL_SONG_SECONDS,
    )

    # The word path answers the song's real voiceless map — the intra-phrase breaths dropped,
    # the nine real stretches kept, to the hundredth of a second.
    assert [(round(low, 2), round(high, 2)) for low, high in from_words] == REAL_VOICELESS
    assert len(interior(from_words)) == 7
    assert len(interior(from_spans)) == 3
    # And *which* four the merge hides, named rather than counted.
    hidden = [gap for gap in interior(from_words) if gap not in interior(from_spans)]
    assert [round(high - low, 2) for low, high in hidden] == [0.74, 0.66, 0.64, 0.72]
    assert all(high - low >= 2 * SNAP_CLEARANCE_SECONDS for low, high in hidden)


def test_malformed_word_timings_cannot_produce_an_inverted_or_negative_gap():
    """Whisper's word times are guaranteed neither ordered nor disjoint, so none is assumed.

    Unsorted, overlapping, zero-length and inverted words in one list. The answer is the same
    ordered, positive-length, non-overlapping map a clean transcript gives: a word that ends
    before it starts contributes nothing rather than a gap running backwards.
    """
    song = measured_song(
        words=[
            ("late", 9.0, 10.0),
            ("early", 2.0, 4.0),
            ("overlapping", 3.5, 6.0),
            ("zero-length", 7.0, 7.0),
            ("inverted", 8.5, 8.0),
        ]
    )

    gaps = vocal_gaps(song, start=0.0, end=12.0)

    assert gaps == [(0.0, 2.0), (6.0, 9.0), (10.0, 12.0)]
    assert all(high > low for low, high in gaps)
    assert all(
        previous[1] < following[0] for previous, following in pairwise(gaps)
    ), "gaps come out ordered and disjoint whatever order the words arrived in"
    # A plan over that song still proposes only targets inside those gaps.
    project = Project(
        name="Malformed",
        song=song,
        shots=[Shot(id="s0", start=0.0, duration=6.0), Shot(id="s1", start=6.0, duration=6.0)],
    )
    for move in snap_cut_plan(project, tolerance=1.5).moves:
        assert any(low <= move.proposed <= high for low, high in gaps)


def test_a_rest_too_short_to_hold_the_clearance_twice_is_not_offered_as_a_gap():
    """`SNAP_MINIMUM_GAP_SECONDS`, and why it belongs in `vocal_gaps` rather than the caller.

    A stretch narrower than twice the clearance cannot hold a cut on this module's own terms,
    and `_gap_snap_target` would answer for it anyway — with its midpoint, which knowingly
    spends less than the clearance the tolerance is tuned around. Worse, it is *near*: the
    nearest-gap rule would prefer a 0.2 s breath between two syllables to the real rest a
    second away. Both halves are asserted: the sliver is not a gap, and a cut beside one snaps
    past it to the rest.
    """
    breath = measured_song(words=[("a", 0.0, 3.0), ("b", 3.2, 6.0)], duration=6.0)
    assert vocal_gaps(breath, start=0.0, end=6.0) == []
    # Exactly twice the clearance is admissible — the floor is a floor, not a margin.
    exact = measured_song(words=[("a", 0.0, 3.0), ("b", 3.3, 6.0)], duration=6.0)
    assert vocal_gaps(exact, start=0.0, end=6.0) == [(3.0, 3.3)]
    assert SNAP_MINIMUM_GAP_SECONDS == 2 * SNAP_CLEARANCE_SECONDS
    # The same floor at both edges of the window, where a sliver is made by *where the window
    # was cut* rather than by the singing — a shot ending 0.2 s after the last word is not a
    # place to put a cut either.
    edges = measured_song(words=[("a", 0.2, 5.8)], duration=6.0)
    assert vocal_gaps(edges, start=0.0, end=6.0) == []
    assert vocal_gaps(edges, start=-0.2, end=6.2) == [(-0.2, 0.2), (5.8, 6.2)]

    project = Project(
        name="Breath beside a rest",
        song=measured_song(
            words=[("a", 0.0, 5.9), ("b", 6.1, 8.0), ("c", 9.0, 20.0)], duration=20.0
        ),
        shots=[Shot(id="s0", start=0.0, duration=6.0), Shot(id="s1", start=6.0, duration=14.0)],
    )

    plan = snap_cut_plan(project, tolerance=3.0)

    # The 0.2 s breath at 5.9–6.1 sits *on* the cut; the real rest is 8.0–9.0, two seconds
    # away. The cut travels to the rest rather than calling the breath a gap.
    assert [move.proposed for move in plan.moves] == [round(8.0 + SNAP_CLEARANCE_SECONDS, 3)]


def test_every_cut_is_skipped_by_name_when_the_measurement_leaves_no_usable_gap():
    """A measured track with nothing voiceless in the plan's window: skips, not an exception.

    `vocal_gaps` answers `[]` — measured, and no room anywhere — which is a different fact
    from `None`, and the plan says so per cut. Reached far more easily now that the words are
    read: a phrase sung straight through has no rest at all in it, where merged spans over the
    same window at least had the merge's own edges.
    """
    project = Project(
        name="Sung throughout",
        song=measured_song(words=[("held", 0.0, 12.0)], duration=12.0),
        shots=[Shot(id="s0", start=0.0, duration=6.0), Shot(id="s1", start=6.0, duration=6.0)],
    )

    plan = snap_cut_plan(project, tolerance=1.5)

    assert plan.status == "ready"
    assert plan.moves == []
    assert [skip.reason for skip in plan.skips] == [
        SNAP_NO_GAP_IN_TOLERANCE.format(
            before=plan.skips[0].before_label,
            after=plan.skips[0].after_label,
            boundary=6.0,
            tolerance=1.5,
        )
    ]
    assert plan.windows == [("s0", 0.0, 6.0), ("s1", 6.0, 6.0)]


def test_each_move_carries_the_length_of_the_gap_it_landed_in():
    """The Director's framing, 2026-08-20: a 1 s gap is an extended shot, a 4 s gap is a plan.

    The report has to say *how long* the gap was or the two read identically. `gap` is the
    length of the stretch the move landed in — the gap the plan actually chose, not one
    re-derived from the boundary — and it is rounded the way every other number in the plan is.
    """
    project = Project(
        name="Two kinds of opportunity",
        song=measured_song(
            words=[("a", 0.0, 5.5), ("b", 6.5, 11.6), ("c", 16.0, 24.0)], duration=24.0
        ),
        shots=[
            Shot(id="s0", start=0.0, duration=5.0),
            Shot(id="s1", start=5.0, duration=6.0),
            Shot(id="s2", start=11.0, duration=13.0),
        ],
    )

    plan = snap_cut_plan(project, tolerance=1.5)

    # Both cuts sit inside a sung phrase and both move. The first finds the one-second breath
    # between "a" and "b"; the second finds the 4.4 s instrumental — and the report can now
    # tell them apart, which is the whole addition.
    assert [(move.boundary, move.proposed, move.gap) for move in plan.moves] == [
        (5.0, round(5.5 + SNAP_CLEARANCE_SECONDS, 3), 1.0),
        (11.0, round(11.6 + SNAP_CLEARANCE_SECONDS, 3), 4.4),
    ]


def test_the_snap_target_clamps_into_the_gap_rather_than_jumping_to_its_middle():
    """The one editorial decision in the module, asserted as a rule rather than as an example.

    A wide gap takes the *smallest* move that clears `SNAP_CLEARANCE_SECONDS` at both ends —
    so a twenty-second instrumental does not demand a ten-second move — while a gap too narrow
    to hold the clearance twice falls back to its midpoint, which is the most clearance it can
    give. A boundary already clear of both edges is its own target, which is what
    `snap_cut_plan` reads as "already lands clear of every sung phrase".
    """
    wide = (10.0, 30.0)

    assert _gap_snap_target(wide, 5.0) == 10.0 + SNAP_CLEARANCE_SECONDS
    assert _gap_snap_target(wide, 40.0) == 30.0 - SNAP_CLEARANCE_SECONDS
    assert _gap_snap_target(wide, 20.0) == 20.0
    narrow = (10.0, 10.2)
    assert _gap_snap_target(narrow, 5.0) == pytest.approx(10.1)
    assert _gap_snap_target(narrow, 40.0) == pytest.approx(10.1)


def test_a_plan_that_is_not_a_contiguous_tiling_has_no_cut_to_move():
    """Two shots that do not share a boundary do not share a cut, so there is nothing single
    to move. Refused by name rather than repaired: closing a hole is an editing decision."""
    with pytest.raises(TimelineError, match="not a contiguous tiling"):
        snap_cut_plan(
            Project(
                name="Holed",
                song=snap_song([(0.5, 7.0)], duration=20.0),
                shots=[
                    Shot(id="s0", start=0.0, duration=6.0),
                    Shot(id="s1", start=7.0, duration=6.0),
                ],
            ),
            tolerance=1.0,
        )
    with pytest.raises(TimelineError, match="not a contiguous tiling"):
        snap_cut_plan(
            Project(
                name="Overlapped",
                song=snap_song([(0.5, 7.0)], duration=20.0),
                shots=[
                    Shot(id="s0", start=0.0, duration=6.0),
                    Shot(id="s1", start=5.0, duration=6.0),
                ],
            ),
            tolerance=1.0,
        )


def test_a_plan_with_fewer_than_two_shots_has_no_cut_at_all():
    project = Project(
        name="One shot",
        song=snap_song([(0.5, 7.0)], duration=20.0),
        shots=[Shot(id="s0", start=0.0, duration=20.0)],
    )

    plan = snap_cut_plan(project, tolerance=1.0)

    assert plan.status == "no_cuts"
    assert plan.message == SNAP_WITHOUT_CUTS.format(count=1)
    assert plan.windows == [("s0", 0.0, 20.0)]


def long_song_plan(duration=154.644898):
    """A full-song plan the length of this application's first live run, exactly contiguous.

    `populate_windows` supplies the *shape* — its own 4–6 s tiling of this exact song — and the
    shots are then built from the shared boundary list rather than from its `(start, duration)`
    pairs, so consecutive shots meet on the same float. That is a fixture decision worth
    stating: `populate_windows` rounds each start and each duration independently and its own
    output can therefore disagree with itself by up to a millisecond, which is inside
    assembly's half-frame tolerance and outside an exactness assertion. The drift a *snap* must
    not introduce is asserted here on an exact plan; that snapping does not make an inexact one
    worse is asserted in the test below.
    """
    raw = populate_windows([], duration, maximum=6.0)
    edges = [round(start, 3) for start, _ in raw] + [round(raw[-1][0] + raw[-1][1], 3)]
    return [
        Shot(id=f"s{index:02d}", start=edges[index], duration=edges[index + 1] - edges[index])
        for index in range(len(edges) - 1)
    ]


def dense_vocal_spans(duration):
    """A realistic voice map: a sung phrase roughly every four seconds, with rests between."""
    return [
        (start, min(start + 2.9, duration))
        for start in [4.0 + index * 4.0 for index in range(int((duration - 8.0) // 4.0))]
    ]


def test_the_snapped_plan_still_tiles_the_whole_song_with_no_gap_overlap_or_drift():
    """Contiguity is the invariant, asserted over a plan long enough for drift to show.

    Thirty-odd windows over the 154.644898 s song of the first live run. Every interior
    boundary is offered a move; afterwards the shots must still meet on the same float, the
    plan's coverage of the song must be the number it was — the two outer boundaries are copied
    through and never assigned — and `assembly.tiling_refusals`, the check that actually gates
    an export, must have nothing to say.
    """
    duration = 154.644898
    project = Project(
        name="Full song",
        song=snap_song(dense_vocal_spans(duration), duration=duration),
        shots=long_song_plan(duration),
    )
    first_start = project.shots[0].start
    last_end = project.shots[-1].end
    assert len(project.shots) >= 25, "the drift assertion needs many cuts to be worth making"

    plan = snap_cut_plan(project, tolerance=SNAP_TOLERANCE_DEFAULT)

    assert plan.status == "ready"
    assert len(plan.moves) >= 10, "the fixture must actually move cuts, or it asserts nothing"
    assert len(plan.moves) + len(plan.skips) == len(project.shots) - 1
    by_id = {shot.id: shot for shot in project.shots}
    for shot_id, start, length in plan.windows:
        by_id[shot_id].start = start
        by_id[shot_id].duration = length

    ordered = ordered_shots(project)
    for previous, current in pairwise(ordered):
        assert previous.end == pytest.approx(current.start, abs=1e-9)
    assert ordered[0].start == first_start
    assert ordered[-1].end == pytest.approx(last_end, abs=1e-9)
    assert sum(shot.duration for shot in ordered) == pytest.approx(
        last_end - first_start, abs=1e-9
    )
    for shot in ordered:
        assert H3_MIN_SHOT_SECONDS - 1e-9 <= shot.duration <= H3_MAX_SHOT_SECONDS + 1e-9
    # The check that actually gates an export, run against the same song the plan covers.
    assert tiling_refusals(
        [
            ClipWindow(
                shot_id=shot.id, label=shot.id, start=shot.start, duration=shot.duration,
                approved_output="", approved_start=0, approved_duration=0, source=None,
            )
            for shot in ordered
        ],
        duration,
    ) == []


def test_snapping_a_plan_that_already_carries_populate_rounding_does_not_make_it_worse():
    """The honest half of the contiguity claim, on the tiling the application actually writes.

    `populate_windows` rounds each start and each duration on its own, so the plan it produces
    can already disagree with itself by up to a millisecond — well inside the half-frame
    assembly allows, and not something a snap is entitled to "fix" by rewriting untouched
    shots. The promise is therefore: every cut this pass *moves* closes exactly, every cut it
    leaves is left exactly as it was, and assembly still has nothing to say about the tiling.
    """
    duration = 154.644898
    project = Project(
        name="Populate's own drift",
        song=snap_song(dense_vocal_spans(duration), duration=duration),
        shots=[
            Shot(id=f"s{index:02d}", start=start, duration=length)
            for index, (start, length) in enumerate(populate_windows([], duration, maximum=6.0))
        ],
    )
    before = {shot.id: (shot.start, shot.duration) for shot in project.shots}

    plan = snap_cut_plan(project, tolerance=SNAP_TOLERANCE_DEFAULT)
    moved_ids = {move.before_id for move in plan.moves} | {
        move.after_id for move in plan.moves
    }
    by_id = {shot.id: shot for shot in project.shots}
    for shot_id, start, length in plan.windows:
        by_id[shot_id].start = start
        by_id[shot_id].duration = length

    assert plan.moves
    # A shot no cut of which moved is bit-identical, which is what keeps an approved shot's
    # window snapshot equal to its live window under assembly's exact comparison.
    for shot in project.shots:
        if shot.id not in moved_ids:
            assert (shot.start, shot.duration) == before[shot.id]
    ordered = ordered_shots(project)
    moved_boundaries = {move.proposed for move in plan.moves}
    for previous, current in pairwise(ordered):
        if current.start in moved_boundaries:
            assert previous.end == pytest.approx(current.start, abs=1e-9)
        else:
            assert abs(previous.end - current.start) <= SNAP_CONTIGUITY_TOLERANCE
    assert tiling_refusals(
        [
            ClipWindow(
                shot_id=shot.id, label=shot.id, start=shot.start, duration=shot.duration,
                approved_output="", approved_start=0, approved_duration=0, source=None,
            )
            for shot in ordered
        ],
        duration,
    ) == []


def test_an_untouched_shot_keeps_its_stored_window_to_the_bit():
    """The ulp guard, stated on its own because the harm it prevents is invisible otherwise.

    `assembly_refusals` compares an approved shot's window snapshot to its live window with
    **exact** inequality. Recomputing every window as a difference of boundaries would rewrite
    an untouched shot by an ulp — behaviourally identical, and enough to make its approval read
    as stale. So an untouched shot's floats are copied, not recomputed.
    """
    project = Project(
        name="Bit-identical",
        song=snap_song([(0.5, 7.0), (8.0, 13.0)], duration=24.3),
        shots=[
            Shot(id="s0", start=0.0, duration=6.0),
            Shot(id="s1", start=6.0, duration=6.0),
            # Far from every cut that can move, and carrying an approval whose snapshot must
            # go on matching its live window exactly.
            Shot(id="s2", start=12.0, duration=6.1, status="approved",
                 approved_output="takes/s2.mp4", approved_start=12.0, approved_duration=6.1),
            Shot(id="s3", start=18.1, duration=6.2),
        ],
    )

    plan = snap_cut_plan(project, tolerance=1.5)
    windows = {shot_id: (start, length) for shot_id, start, length in plan.windows}

    assert plan.moves, "a fixture with no move proves nothing about what a move leaves alone"
    # `s3` touches no moved cut: its stored floats come back unchanged, not recomputed.
    assert windows["s3"] == (18.1, 6.2)
    assert windows["s2"] == (12.0, 6.1)
    approved = project.shots[2]
    assert windows["s2"] == (approved.approved_start, approved.approved_duration)


def test_the_band_is_judged_against_the_boundary_the_previous_cut_already_settled_on():
    """The exactness argument in `snap_cut_plan`'s docstring, made falsifiable.

    Cuts are decided left to right, and the left edge of the shot before a cut is a boundary
    an *earlier* cut may already have moved. Judging the band against the shot's original
    start instead would approve a move on a length that no longer exists.

    Here the 5.000 s cut moves right to 6.000 s, so `s1` is 4.500 s long before the second cut
    is even considered. The 10.500 s cut then wants to move left to 9.900 s, which would leave
    `s1` at 3.900 s — under the floor, and refused. Against `s1`'s *original* 5.000 s start the
    same move computes 4.900 s and sails through, which is the defect this asserts against.
    """
    project = Project(
        name="Left edge already moved",
        song=snap_song([(0.3, 5.85), (7.0, 9.75), (10.05, 18.0)], duration=20.0),
        shots=[
            Shot(id="s0", start=0.0, duration=5.0),
            Shot(id="s1", start=5.0, duration=5.5),
            Shot(id="s2", start=10.5, duration=9.5),
        ],
    )

    plan = snap_cut_plan(project, tolerance=1.0)

    assert [(move.boundary, move.proposed) for move in plan.moves] == [(5.0, 6.0)]
    refused = next(skip for skip in plan.skips if skip.boundary == 10.5)
    # 3.900 s, not 4.900 s: the length is measured from the boundary the first cut settled on.
    assert "3.900s" in refused.reason
    assert "4.900s" not in refused.reason
    assert "under the 4s minimum" in refused.reason
    assert plan.windows == [("s0", 0.0, 6.0), ("s1", 6.0, 4.5), ("s2", 10.5, 9.5)]
