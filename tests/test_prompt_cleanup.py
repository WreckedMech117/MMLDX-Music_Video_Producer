"""Asset labels out of the prose: the structural citation field, and the cleanup pass.

Two halves of one defect the Director reported on their live plan (2026-08-21): 24 of the
prompts name a non-character asset, because a citation was only ever created when the model
typed that asset's display name into the shot's creative writing.

* **Half A** — `director.PlannedShot.assets` gives the model a structural place to cite from,
  promoted into the strict grammar, with the prose scan demoted to a fallback. For plans made
  from now on.
* **Half B** — `POST /timeline/clean-prompts` rewrites the *prose only* of an existing plan.
  For the plan the Director already has.

**The constraint that dominates Half B is that the windows are sacred.** The Director's own
words: *"I have done some timeline touch ups and am generally happy for now."* Their 33 shots
carry hand-placed edges including six deliberate micro-cuts, and that work is irreplaceable.
`DIRECTOR_PLAN` below is that plan, verbatim from `data/projects/project_59f14d19ff10`, and
every geometry assertion in this file is made against it rather than against a tidy invention.

**No live model validated any of the rewrite wording in this file.** The doubles here return
text a human wrote; the system prompt has never been sent to
`gemma-4-26b-a4b-it-heretic-ara-v2` or to anything else. What is tested is the machinery — what
is asked for, what is accepted, what is refused, and what is left alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from music_video_producer.app import create_app
from music_video_producer.config import Settings
from music_video_producer.models import (
    Asset,
    AssetCitation,
    Project,
    Shot,
    Song,
    SongSection,
    assets_for_proposal,
)
from music_video_producer.prompt_cleanup import (
    CLEANUP_REJECT_EMPTY,
    CLEANUP_REJECT_GUTTED,
    CLEANUP_REJECT_JSON,
    ECHO_EXEMPT_KINDS,
    citation_fingerprint,
    echoed_labels,
    prompt_cleanup_input,
    rewrite_rejection,
    window_fingerprint,
)
from music_video_producer.store import ProjectStore


class FakeComfy:
    """The no-GPU double. Nothing in this feature may touch it; `prompts` staying empty on
    every path — report, apply, refusal, populate — is the claim every test here makes."""

    def __init__(self):
        self.prompts: list[dict] = []

    async def health(self):
        return {"online": True, "url": "http://fake"}

    async def submit(self, prompt, client_id=None):  # pragma: no cover - must never run
        self.prompts.append(prompt)
        raise AssertionError("nothing in this feature may submit to ComfyUI")

    async def queue(self):
        return {"queue_running": [], "queue_pending": []}

    async def history(self, prompt_id):  # pragma: no cover - must never run
        raise AssertionError("nothing in this feature may read ComfyUI history")


#: The live project's library, name and kind, exactly as the manifest holds it. The character
#: carries a promotion suffix; every other name is a label for a picture of a thing.
DIRECTOR_ASSETS = [
    ("Dusk Warehouse Bed", "setting"),
    ("HarderFaster · multiview", "character"),
    ("Vintage Chrome Mic", "prop"),
    ("Gritty Warehouse Floor", "setting"),
    ("Blue Haze Atmosphere", "style"),
    ("Crimson Lips Close-up", "prop"),
    ("Silver Spiked Choker", "prop"),
]

#: The Director's 33 shots: id, start, duration, prompt — in **manifest** order, which is not
#: song order (the last four sit at the end of the file and play at 0 s, 62.75 s, 64.58 s and
#: 107.5 s). Read straight out of `data/projects/project_59f14d19ff10/project.json`.
#:
#: The micro-cuts are the point: 0.5 s, 1.75 s, 1.833 s, 1.875 s, 2.083 s and 2.667 s. Nothing
#: a tiling function would ever produce, and nothing any test here is allowed to disturb.
DIRECTOR_PLAN = [
    ("shot_59f8da92c2d8", 6.208, 4.792, "Moonlight hits the Gritty Warehouse Floor in deep shadows."),
    ("shot_320c841cdad0", 11.0, 5.042, "HarderFaster sings aggressively at a Vintage Chrome Mic with handheld camera shake."),
    ("shot_976ca97baa38", 16.0, 5.333, "HarderFaster moves through the shadows near the Gritty Warehouse Floor."),
    ("shot_6a6137220f63", 21.333, 2.083, "Close up of Silver Spiked Choker on HarderFaster as she runs her hand across her throat."),
    ("shot_aa9f610512f6", 23.417, 5.833, "Extreme close up of Crimson Lips Close-up while HarderFaster performs."),
    ("shot_480e52d6fbb4", 29.25, 3.292, "Extreme close up of Crimson Lips Close-up while HarderFaster performs."),
    ("shot_c8efd34a8333", 32.54, 5.375, "HarderFaster stands in the Blue Haze Atmosphere looking wild."),
    ("shot_22e4f463ec82", 37.917, 4.917, "Slow pan across the silk textures of the Dusk Warehouse Bed as HarderFaster rolls around."),
    ("shot_c82073f44646", 42.792, 8.083, "Close up of Crimson Lips Close-up on HarderFaster's face singing as she lays on the bed."),
    ("shot_25b661593524", 50.875, 5.5, "HarderFaster arches sensually on the Dusk Warehouse Bed."),
    ("shot_cddf204f1fc3", 56.36, 4.667, "HarderFaster sings into the Vintage Chrome Mic with sensual energy."),
    ("shot_14ff133430ab", 61.0, 1.75, "Glamour shot of HarderFaster posing on the bed."),
    ("shot_22fd7b5e0388", 66.379, 5.01, "Silver Spiked Choker catches a sharp blue moonlight beam as the camera pans up to HarderFasters face as she sings."),
    ("shot_a3f2381b257f", 71.375, 5.811, "HarderFaster singing with intense emotion, handheld camera movement."),
    ("shot_3133c0d92d6e", 77.208, 5.125, "High angle view of HarderFaster on the Dusk Warehouse Bed."),
    ("shot_b30f8d654877", 82.292, 5.167, "Close up of eyes of HarderFaster reflecting moonlight."),
    ("shot_b9afc803c2d4", 87.417, 8.542, "The camera glides over the dark silk of the Dusk Warehouse Bed with HarderFaster laying across it seductively."),
    ("shot_e17c546b7dae", 95.958, 2.667, "Close Up, HarderFaster seductively singing in the moonlight on the bed."),
    ("shot_6fa420d811aa", 98.583, 4.625, "Blue Haze Atmosphere surrounding the Dusk Warehouse Bed."),
    ("shot_1f151cd451a4", 103.2, 4.333, "Blurry handheld shot of HarderFaster singing in a frenzy."),
    ("shot_f7296453a359", 108.583, 5.102, "Extreme close up of Crimson Lips Close-up, smeared and wet."),
    ("shot_9a1152cf52b3", 113.686, 5.027, "Handheld camera moves through the Blue Haze Atmosphere."),
    ("shot_523484d02824", 118.713, 4.57, "Flickering light glints off the Silver Spiked Choker."),
    ("shot_7e6765d418dd", 123.25, 5.625, "Slow zoom out from the empty Dusk Warehouse Bed with HarderFaster on her knees singing to the camera."),
    ("shot_3ddc0efaf2ac", 128.833, 5.292, "HarderFaster sings softly directly into the lens."),
    ("shot_98e4f28a3dd4", 134.113, 5.007, "The Vintage Chrome Mic stands alone in a single spotlight as the camera pulls away from it."),
    ("shot_3bb6cace4d81", 139.12, 5.167, "Wide shot of Blue Haze Atmosphere and deep shadows."),
    ("shot_31e34ce7c84a", 149.133, 5.507, "Moonlight fades on the Gritty Warehouse Floor, HarderFaster walking away from the camera."),
    ("shot_47a3ecea1947", 144.25, 10.375, "Moonlight fades on the Gritty Warehouse Floor as HarderFaster walks away from the camera."),
    ("shot_726472b14106", 0.0, 6.208, "Wide shot of Blue Haze Atmosphere drifting through a dark warehouse."),
    ("shot_6026dc48c7ba", 62.75, 1.833, "Glamour shot of HarderFaster posing on the bed."),
    ("shot_74c918882929", 64.583, 1.875, "Glamour shot of HarderFaster posing on the bed."),
    ("shot_1570bb6298ac", 107.5, 0.5, "Close up Blurry handheld shot of HarderFaster running her face down her upper chest"),
]

#: The Director's own count, reproduced here so a change to the scan that quietly stopped
#: seeing a label fails loudly rather than by reporting fewer rows.
DIRECTOR_ECHOING = 24

DIRECTOR_SECTIONS = [
    ("Intro", 0.0, 11.0),
    ("Verse", 11.0, 21.54),
    ("Chorus", 32.54, 23.82),
    ("Verse 2", 56.36, 20.84),
    ("Chorus 2", 77.2, 26.0),
    ("Bridge", 103.2, 20.9),
    ("Outro", 124.1, 30.54),
]

LIVE_PROJECT = Path("data/projects/project_59f14d19ff10/project.json")


class CleanupDirector:
    """A director double whose rewrites the test chooses, request recorded.

    Recording rather than fixed, because the *input* is half the contract: this pass is shown
    the shots it is meant to fix, each with the labels it must lose, and a double that
    swallowed `expansion_input` would let the labels stop being sent without a test noticing.
    The system prompt is recorded for the same reason — it is what selects the persona.

    `rewrites` maps shot id to the prose to return, or is a callable handed the input so a test
    can answer per shot. Ids not in it get no entry at all, which is one of the failure modes
    the route has to survive.

    `extra` appends raw `(shot_id, prompt)` pairs after the mapped ones, so a test can send a
    duplicate id or an id no shot has.
    """

    def __init__(self, rewrites=None, extra=(), message="Labels removed."):
        self.rewrites = rewrites or {}
        self.extra = list(extra)
        self.message = message
        self.calls: list[dict] = []

    async def expand(self, *, expansion_input, system_prompt=None):
        self.calls.append({"input": expansion_input, "system_prompt": system_prompt})
        chosen = (
            self.rewrites(expansion_input)
            if callable(self.rewrites)
            else dict(self.rewrites)
        )
        entries = [*chosen.items(), *self.extra]
        return type(
            "ShotExpansion",
            (),
            {
                "message": self.message,
                "shots": [
                    type("ExpandedShot", (), {"shot_id": shot_id, "prompt": prompt})()
                    for shot_id, prompt in entries
                ],
            },
        )()


class PlanningDirector:
    """A populate double whose proposals the test chooses, request recorded.

    Deliberately built with `type(...)` rather than with real `PlannedShot`s, so a test can
    hand the route a proposal object that carries **no** `assets` attribute at all — which is
    the shape a provider that ignores the strict schema produces, and the shape every double
    written before this field existed has. That absence is what the byte-identity test pins.
    """

    def __init__(self, shots, message="Here is the plan."):
        self.shots = shots
        self.message = message
        self.requests: list[dict] = []

    async def plan(self, message, project_context, temperature=None, response_schema=None):
        self.requests.append({"message": message, "context": project_context})
        return type(
            "DirectorResult",
            (),
            {
                "message": self.message,
                "treatment": "",
                "style_bible": "",
                "shots": [
                    type("PlannedShot", (), dict(fields))() for fields in self.shots
                ],
                "sections": [],
            },
        )()


def make_client(tmp_path: Path, director):
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    comfy = FakeComfy()
    app = create_app(settings=settings, store=store, comfy=comfy, director=director)
    return TestClient(app), store, comfy


def director_project(store: ProjectStore, **fields) -> Project:
    """The Director's live plan, rebuilt: their library, their 33 windows, their prose.

    Citations are the ones the manifest holds in spirit rather than byte for byte — every shot
    cites the assets its prose names, plus the declared location — because what the cleanup
    guarantees is that whatever they are, they do not move. Building them from the same scan
    the plan was populated by is the honest way to get a realistic set.
    """
    project = store.create(Project(name="Harder Faster"))
    project.song = Song(
        title="Harder Faster", source="imported", path="song.mp3", duration=154.644898
    )
    project.treatment = "A performer alone in a moonlit warehouse, from swagger to collapse."
    project.style_bible = "Deep midnight blues, oxblood red, chiaroscuro, 35mm grain."
    media = store.media_dir(project.id)
    project.assets = []
    for index, (name, kind) in enumerate(DIRECTOR_ASSETS):
        path = media / f"asset{index}.png"
        path.write_bytes(b"png")
        project.assets.append(
            Asset(
                id=f"asset_{index}",
                name=name,
                kind=kind,
                path=f"media/asset{index}.png",
            )
        )
    project.default_setting_id = "asset_0"
    project.sections = [
        SongSection(label=label, start=start, duration=duration)
        for label, start, duration in DIRECTOR_SECTIONS
    ]
    project.shots = [
        Shot(
            id=shot_id,
            start=start,
            duration=duration,
            prompt=prompt,
            singing="singing" if "sing" in prompt.lower() else "not_singing",
            use_song_audio=True,
            seed=index + 1,
            citations=[
                AssetCitation(asset_id=asset.id, role="reference", order=order)
                for order, asset in enumerate(
                    assets_for_proposal(project.assets, prose=prompt)
                )
            ],
        )
        for index, (shot_id, start, duration, prompt) in enumerate(DIRECTOR_PLAN)
    ]
    for key, value in fields.items():
        setattr(project, key, value)
    return store.save(project)


def clean(client, project_id: str, **body):
    return client.post(f"/api/projects/{project_id}/timeline/clean-prompts", json=body)


def repopulate(client, project_id: str):
    return client.post(
        f"/api/projects/{project_id}/timeline/populate", json={"confirm_replace": True}
    )


def blank_plan(store: ProjectStore, project: Project, *, located: bool = True) -> Project:
    """The Director's library and song with no shots and no sections — populate's own input.

    Populate is destructive by design and refuses over any protection, so these tests give it
    an empty timeline. The Director's *plan* is never handed to it anywhere in this file: that
    is the point of Half B existing at all.
    """
    project.shots = []
    project.sections = []
    if not located:
        project.default_setting_id = ""
    return store.save(project)


def proposals(count: int, **fields) -> list[dict]:
    """`count` identical proposal field-dicts, enough to satisfy `populate_required_shots`."""
    return [{"start": index * 5.0, "duration": 5.0, **fields} for index in range(count)]


def _strip_all(payload: dict) -> list[tuple[str, str]]:
    """A rewrite per sent shot that lowercases every label it was told to lose.

    The dumbest legitimate fix there is, and it is a real one: what makes "the Silver Spiked
    Choker" read as a catalogue entry is the capitalisation. Used where a test needs *some*
    acceptable answer for every shot and does not care what it says.
    """
    rewrites = []
    for entry in payload["shots"]:
        text = entry["prompt"]
        for label in entry["labels"]:
            text = text.replace(label, label.lower())
        rewrites.append((entry["shot_id"], text))
    return rewrites


def plain_rewrites(project: Project) -> dict[str, str]:
    """A hand-written rewrite for every echoing shot: the label gone, everything else kept.

    Written by a person, not by a model. This is what the double returns, and the tests here
    assert what the route *does* with it — never that a model would produce it.
    """
    swaps = {
        "Gritty Warehouse Floor": "cracked concrete floor",
        "Vintage Chrome Mic": "vintage chrome microphone",
        "Silver Spiked Choker": "silver spiked choker",
        "Crimson Lips Close-up": "smeared crimson lips",
        "Blue Haze Atmosphere": "drifting blue haze",
        "Dusk Warehouse Bed": "dusk-lit warehouse bed",
    }
    rewrites: dict[str, str] = {}
    for shot in project.shots:
        text = shot.prompt
        for label, replacement in swaps.items():
            text = text.replace(label, replacement)
        if text != shot.prompt:
            rewrites[shot.id] = text
    return rewrites


# --------------------------------------------------------------------------------------
# Half A — the mechanism
# --------------------------------------------------------------------------------------


def test_a_shot_that_declares_its_assets_is_cited_from_the_field_not_from_its_prose(
    tmp_path: Path,
):
    """The whole of Half A in one assertion: prose that names nothing, citations that do.

    This is the shape the defect made impossible. Before `PlannedShot.assets` existed, a shot
    whose prompt did not spell "Vintage Chrome Mic" could not cite the microphone at all — the
    scan was the mechanism — so every citation was paid for with a word in the creative
    writing. Here the prompt is a director's sentence and the attachment is data.
    """
    from music_video_producer.app import populate_required_shots

    director = PlanningDirector(
        shots=proposals(
            populate_required_shots(154.644898),
            prompt="She leans into a battered chrome microphone, half in shadow.",
            performance=True,
            assets=["Vintage Chrome Mic", "HarderFaster · multiview"],
        )
    )
    client, store, comfy = make_client(tmp_path, director)
    project = blank_plan(store, director_project(store))

    assert repopulate(client, project.id).status_code == 200
    saved = store.get(project.id)
    for shot in saved.shots:
        cited = {citation.asset_id for citation in shot.citations}
        # Both declared assets are attached...
        assert {"asset_2", "asset_1"} <= cited
        # ...and not one library label appears in the prose that produced them.
        for asset in saved.assets:
            assert asset.name.lower() not in shot.prompt.lower()
    assert comfy.prompts == []


def test_the_prose_scan_still_catches_an_asset_the_field_left_out(tmp_path: Path):
    """The fallback rule, and the reason it is not optional.

    A model that writes a name into the prose and omits it from `assets` has said
    unambiguously which picture it wants. Dropping that citation would send the shot to render
    with no reference — invisible until the take comes back wrong — where keeping it costs an
    awkward sentence a human can read and fix. **Losing a citation is the worse failure**, so
    the scan stays, demoted to a fallback and never removed.
    """
    from music_video_producer.app import populate_required_shots

    director = PlanningDirector(
        shots=proposals(
            populate_required_shots(154.644898),
            prompt="The Silver Spiked Choker catches the light.",
            performance=False,
            # Declares one asset; writes a different one into the prose.
            assets=["Vintage Chrome Mic"],
        )
    )
    client, store, comfy = make_client(tmp_path, director)
    project = blank_plan(store, director_project(store))

    assert repopulate(client, project.id).status_code == 200
    saved = store.get(project.id)
    for shot in saved.shots:
        cited = [citation.asset_id for citation in shot.citations]
        # Declared first, then the prose match — the order is the rule, not an accident.
        assert cited[:2] == ["asset_2", "asset_6"]
    assert comfy.prompts == []


def test_a_name_declared_and_written_is_cited_exactly_once(tmp_path: Path):
    """Both halves together, and no double citation.

    The failure this forbids is not cosmetic. H3 wires nine pictures and no more
    (`H3_REFERENCE_LIMITS`), so a shot that cited one asset twice would spend two of those
    slots on one picture — and `prefer_identity_sheets` refuses to create exactly that waste
    for exactly that reason.
    """
    from music_video_producer.app import populate_required_shots

    director = PlanningDirector(
        shots=proposals(
            populate_required_shots(154.644898),
            prompt="The Silver Spiked Choker catches the light.",
            performance=False,
            # Named three times over: twice by name, once by id, and once again in the prose.
            assets=["Silver Spiked Choker", "Silver Spiked Choker", "asset_6"],
        )
    )
    client, store, comfy = make_client(tmp_path, director)
    project = blank_plan(store, director_project(store))

    assert repopulate(client, project.id).status_code == 200
    saved = store.get(project.id)
    for shot in saved.shots:
        cited = [citation.asset_id for citation in shot.citations]
        assert cited.count("asset_6") == 1
        assert len(cited) == len(set(cited))
    assert comfy.prompts == []


def test_a_plan_with_no_structural_field_produces_exactly_what_it_always_did(
    tmp_path: Path,
):
    """Byte-identity, pinned. The compatibility claim this whole change rests on.

    `PlanningDirector` builds proposals with `type(...)`, so these objects have no `assets`
    attribute at all — which is what every double written before today has, and what a provider
    ignoring the strict schema returns. On that path the route must produce the citations the
    old prose scan produced, in the old order, with the old `order` values, and must not touch
    the prose.

    Asserted against the old expression *spelled out here*, not against a helper, so a change
    to the helper cannot quietly redefine what "what it always did" means.
    """
    prompts = [
        "Moonlight hits the Gritty Warehouse Floor in deep shadows.",
        "She sings into a Vintage Chrome Mic, handheld and close.",
        "Nothing from the library appears in this one at all.",
    ]
    from music_video_producer.app import populate_required_shots

    director = PlanningDirector(
        shots=[
            {"start": index * 5.0, "duration": 5.0, "prompt": prompts[index % 3]}
            for index in range(populate_required_shots(154.644898))
        ]
    )
    client, store, comfy = make_client(tmp_path, director)
    # No declared location either, so `with_default_setting` is a no-op and the citation list
    # is the raw scan and nothing else.
    project = blank_plan(store, director_project(store), located=False)

    assert repopulate(client, project.id).status_code == 200
    saved = store.get(project.id)
    for shot in saved.shots:
        lowered = shot.prompt.lower()
        expected = [
            AssetCitation(asset_id=asset.id, role="reference", order=order)
            for order, asset in enumerate(
                asset
                for asset in saved.assets
                if len(asset.name) >= 4 and asset.name.lower() in lowered
            )
        ]
        assert shot.citations == expected
        assert shot.asset_ids == [citation.asset_id for citation in expected]
        # The prose is the proposal's, character for character.
        assert shot.prompt in prompts
    assert comfy.prompts == []


def test_assets_for_proposal_matches_by_id_and_by_exact_name_and_drops_the_rest():
    """The resolver's rules, at the unit.

    An unresolvable entry is dropped rather than guessed at: it cannot invent an asset, and
    guessing which asset a near-miss meant is how a shot ends up conditioned on the wrong face.
    A near-miss is exactly where the *prose* fallback earns its place instead.
    """
    library = [
        Asset(id="asset_bed", name="Dusk Warehouse Bed", kind="setting", path="a.png"),
        Asset(id="asset_mic", name="Vintage Chrome Mic", kind="prop", path="b.png"),
    ]

    assert [a.id for a in assets_for_proposal(library, declared=["asset_mic"])] == [
        "asset_mic"
    ]
    # Exact name, case and surrounding space forgiven — the model copied it off a roster.
    assert [
        a.id for a in assets_for_proposal(library, declared=["  vintage chrome mic "])
    ] == ["asset_mic"]
    # A near miss resolves to nothing. It is not "Vintage Chrome Microphone".
    assert assets_for_proposal(library, declared=["Vintage Chrome Microphone"]) == []
    # Declared order is kept; it is the model's own ordering of its own shot.
    assert [
        a.id for a in assets_for_proposal(library, declared=["asset_mic", "asset_bed"])
    ] == ["asset_mic", "asset_bed"]
    # And with nothing declared, it is the old scan exactly.
    assert [
        a.id for a in assets_for_proposal(library, prose="She lies on the Dusk Warehouse Bed.")
    ] == ["asset_bed"]
    assert assets_for_proposal(library, prose="Nothing here.") == []


def test_assets_for_proposal_never_cites_one_asset_twice(tmp_path: Path):
    """De-duplication at the unit, because downstream it is invisible.

    `prefer_identity_sheets` collapses duplicate `(asset_id, role)` pairs on its way past, so a
    resolver that cited one asset twice would be silently repaired by the time a Shot exists —
    and a route-level test of this rule proves nothing. Found by mutation: removing the `seen`
    check from either loop left the whole suite green.

    The cost it prevents is real: H3 wires nine pictures and no more, and two of them on one
    picture is the waste `prefer_identity_sheets` exists to refuse.
    """
    library = [
        Asset(id="asset_bed", name="Dusk Warehouse Bed", kind="setting", path="a.png"),
        Asset(id="asset_mic", name="Vintage Chrome Mic", kind="prop", path="b.png"),
    ]

    # The same asset named twice inside the field — by name, then by id.
    assert [
        a.id
        for a in assets_for_proposal(
            library, declared=["Vintage Chrome Mic", "asset_mic", "vintage chrome mic"]
        )
    ] == ["asset_mic"]
    # And named once in the field and again in the prose: one citation, and it keeps the
    # field's position rather than being re-added at the end.
    assert [
        a.id
        for a in assets_for_proposal(
            library,
            declared=["asset_mic"],
            prose="The Vintage Chrome Mic beside the Dusk Warehouse Bed.",
        )
    ] == ["asset_mic", "asset_bed"]


def test_the_prose_scan_stays_case_insensitive_and_keeps_its_short_name_floor():
    """The fallback's two properties, pinned because Half A must not change them.

    Case-insensitivity is what makes the byte-identity claim true — this is the scan that has
    always built citations, and tightening it would silently drop references from every plan
    already on disk. The four-character floor is what stops a plain substring test from
    matching inside ordinary words.

    Note the deliberate asymmetry with `prompt_cleanup.echoed_labels`, which *is*
    case-sensitive: that one is asking "does this sentence read like a catalogue entry", and
    this one is asking "which picture did they mean". Different questions.
    """
    library = [
        Asset(id="asset_bed", name="Dusk Warehouse Bed", kind="setting", path="a.png"),
        Asset(id="asset_aj", name="AJ", kind="character", path="b.png"),
        Asset(id="asset_lit", name="Lit", kind="style", path="c.png"),
    ]

    assert [
        a.id for a in assets_for_proposal(library, prose="she lies on the dusk warehouse bed.")
    ] == ["asset_bed"]
    # Two and three characters are substring noise: "AJ" is inside "AJAR", "Lit" inside
    # "Literally" and inside "moonlit". Neither may cite.
    assert assets_for_proposal(library, prose="The door is AJAR and moonlit.") == []
    # But the field may name them, because an exact name is exact at any length.
    assert [a.id for a in assets_for_proposal(library, declared=["AJ", "Lit"])] == [
        "asset_aj",
        "asset_lit",
    ]


def test_a_manifest_written_before_the_field_existed_still_loads(tmp_path: Path):
    """The other half of compatibility: nothing on disk gained a required field.

    `PlannedShot` is a wire model and never reaches a manifest, so the risk is the reverse one
    — a project saved yesterday must still load, and every new field must default. Asserted by
    round-tripping the Director's own plan through a store that has never seen the new code's
    output.
    """
    store = ProjectStore(tmp_path)
    project = director_project(store)
    reread = ProjectStore(tmp_path).get(project.id)
    assert len(reread.shots) == 33
    assert [shot.id for shot in reread.shots] == [row[0] for row in DIRECTOR_PLAN]


# --------------------------------------------------------------------------------------
# Half B — the pure decisions
# --------------------------------------------------------------------------------------


def test_echoed_labels_finds_the_directors_own_twenty_four_and_spares_the_performer():
    """The scan, measured against the number the Director measured.

    24 of 33. The character's name is exempt and that is the Director's own line — their
    quoted bad example is bad in its first half only ("Extreme close up of **Crimson Lips
    Close-up** while HarderFaster performs"), and a prose pass that stripped the performer's
    name would leave the shot with no subject.
    """
    library = [
        Asset(id=f"asset_{index}", name=name, kind=kind, path="a.png")
        for index, (name, kind) in enumerate(DIRECTOR_ASSETS)
    ]
    echoing = [row for row in DIRECTOR_PLAN if echoed_labels(row[3], library)]
    assert len(echoing) == DIRECTOR_ECHOING

    assert echoed_labels(
        "Extreme close up of Crimson Lips Close-up while HarderFaster performs.", library
    ) == ["Crimson Lips Close-up"]
    # Two labels in one sentence, in library order.
    assert echoed_labels("Blue Haze Atmosphere surrounding the Dusk Warehouse Bed.", library) == [
        "Dusk Warehouse Bed",
        "Blue Haze Atmosphere",
    ]
    # The performer is never an echo, alone or beside one.
    assert echoed_labels("HarderFaster sings softly directly into the lens.", library) == []
    assert ECHO_EXEMPT_KINDS == frozenset({"character"})


def test_echoed_labels_spares_a_plainly_named_character_and_respects_case_and_length():
    """The three properties of the scan, each on data that can actually distinguish them.

    All three were mutation survivors against the live library, and each for a different
    reason worth recording:

    * the character in the Director's project is called "HarderFaster · multiview" and no
      prompt spells the suffix, so dropping the exemption changed nothing there. The case it
      is *for* is an ordinary character asset named "HarderFaster", which is what a project
      with no promotion has — and stripping the performer's name out of every prompt would be
      a far worse defect than the one being fixed;
    * no asset in that library is under four characters;
    * and every echo in it is already exact-case.
    """
    library = [
        Asset(id="asset_her", name="HarderFaster", kind="character", path="a.png"),
        Asset(id="asset_bed", name="Dusk Warehouse Bed", kind="setting", path="b.png"),
        Asset(id="asset_short", name="Bed", kind="prop", path="c.png"),
    ]

    # The performer's own name, in plain prose, is never an echo.
    assert echoed_labels("HarderFaster sings into the lens.", library) == []
    assert echoed_labels(
        "HarderFaster arches sensually on the Dusk Warehouse Bed.", library
    ) == ["Dusk Warehouse Bed"]
    # Case-sensitive: the lowercase form is already description and is left alone. This is the
    # pairing that lets a rewrite fix a label by lowercasing it.
    assert echoed_labels("She arches on the dusk warehouse bed.", library) == []
    # And the short-name floor, on the one shape that can actually exercise it: a name under
    # four characters, matching *with its own capitalisation* inside an ordinary word. "Bed" is
    # the first three letters of "Bedroom", and reporting that as a label the Director wrote
    # would send a perfectly good sentence to be rewritten.
    assert echoed_labels("Bedroom lit by one lamp, she sings.", library) == []


def test_rewrite_rejection_refuses_an_empty_a_json_and_a_gutted_answer_and_a_no_op():
    """The checker, branch by branch. Everything it refuses is reported, never swallowed."""
    original = "Extreme close up of Crimson Lips Close-up while HarderFaster performs."
    labels = ["Crimson Lips Close-up"]

    assert rewrite_rejection("   ", original=original, labels=labels) == CLEANUP_REJECT_EMPTY
    assert (
        rewrite_rejection('{"prompt": "x"}', original=original, labels=labels)
        == CLEANUP_REJECT_JSON
    )
    # The one that matters: a rewrite that still says the label is a call that did nothing.
    still = rewrite_rejection(
        "Extreme close up of the Crimson Lips Close-up, in moonlight.",
        original=original,
        labels=labels,
    )
    assert "Crimson Lips Close-up" in still
    # **Case-sensitive, deliberately.** Lowercasing a proper-noun label into an ordinary noun
    # phrase is one of the best fixes available, and a case-insensitive check would forbid it:
    # measured against the Director's own plan, it rejected 6 of the 24 rewrites and every one
    # of them was correct.
    assert (
        rewrite_rejection(
            "Flickering light glints off the silver spiked choker.",
            original="Flickering light glints off the Silver Spiked Choker.",
            labels=["Silver Spiked Choker"],
        )
        == ""
    )
    # And the prefix case the same measurement found: "vintage chrome microphone" contains
    # "vintage chrome mic" case-insensitively and is plainly a fix.
    assert (
        rewrite_rejection(
            "HarderFaster sings into the vintage chrome microphone with sensual energy.",
            original="HarderFaster sings into the Vintage Chrome Mic with sensual energy.",
            labels=["Vintage Chrome Mic"],
        )
        == ""
    )
    # A deletion dressed as a rewrite.
    assert CLEANUP_REJECT_GUTTED.split("{")[0] in rewrite_rejection(
        "Extreme close up.", original=original, labels=labels
    )
    # And the good answer passes.
    assert (
        rewrite_rejection(
            "Extreme close up of smeared crimson lips while HarderFaster performs.",
            original=original,
            labels=labels,
        )
        == ""
    )


def test_the_cleanup_input_sends_only_the_echoing_shots_with_their_labels(tmp_path: Path):
    """The payload, and the selection it encodes.

    A shot whose prose is already clean is not a question the model should be asked. Including
    it would invite a rewrite of a prompt nobody complained about, which on a hand-reviewed
    plan is how 24 needed changes quietly become 33.

    The library is deliberately *not* described in this payload: a roster here would be a fresh
    list of names to echo, which is the defect in a new room.
    """
    store = ProjectStore(tmp_path)
    project = director_project(store)
    echoes = {
        shot.id: echoed_labels(shot.prompt, project.assets)
        for shot in project.shots
        if echoed_labels(shot.prompt, project.assets)
    }
    payload = prompt_cleanup_input(project, echoes)

    assert len(payload["shots"]) == DIRECTOR_ECHOING
    assert set(payload) == {"treatment", "style_bible", "shots"}
    for entry in payload["shots"]:
        assert set(entry) <= {"shot_id", "prompt", "labels", "section"}
        assert entry["labels"]
        # No window travels. This pass may not retime and does not need to know.
        assert "start" not in entry and "duration" not in entry
    # Song order, which is not manifest order for this plan.
    starts = {shot.id: shot.start for shot in project.shots}
    sent = [starts[entry["shot_id"]] for entry in payload["shots"]]
    assert sent == sorted(sent)


def test_window_fingerprint_notices_a_move_a_reorder_and_a_count_change(tmp_path: Path):
    """The guarantee's instrument, tested for the three ways geometry can change.

    Manifest order rather than song order is load-bearing: `shot_label` numbers the timeline by
    manifest position, so a reorder renames every clip on screen even when no window moved.
    """
    store = ProjectStore(tmp_path)
    project = director_project(store)
    before = window_fingerprint(project)

    assert len(before) == 33
    assert before == window_fingerprint(store.get(project.id))

    moved = store.get(project.id)
    moved.shots[0].start += 0.001
    assert window_fingerprint(moved) != before

    # Duration on its own, checked separately: a fingerprint of starts alone would call a
    # shot that grew by three seconds unchanged, and "the windows did not move" would then be
    # true of a plan whose edges had all shifted. Found by mutation.
    stretched = store.get(project.id)
    stretched.shots[0].duration += 0.001
    assert window_fingerprint(stretched) != before

    reordered = store.get(project.id)
    reordered.shots.reverse()
    assert window_fingerprint(reordered) != before

    shorter = store.get(project.id)
    shorter.shots.pop()
    assert window_fingerprint(shorter) != before

    # And a prompt change — the only change this route makes — moves it not at all.
    reworded = store.get(project.id)
    reworded.shots[0].prompt = "Something else entirely."
    assert window_fingerprint(reworded) == before


def test_citation_fingerprint_is_ordered_and_carries_role_and_order(tmp_path: Path):
    """The other guard's instrument, and it is sequence-sensitive on purpose.

    `order` sorts within a role and the stored sequence is what `citations_in_prompt_order`
    walks, so a reshuffle changes which picture a prompt's numbered tags refer to even when the
    set of ids is identical. A set-based fingerprint would call that unchanged — which is how
    it survived mutation until this test existed.
    """
    store = ProjectStore(tmp_path)
    project = director_project(store)
    shot = next(s for s in project.shots if len(s.citations) >= 2)
    before = citation_fingerprint(shot)

    reshuffled = shot.model_copy(deep=True)
    reshuffled.citations.reverse()
    assert citation_fingerprint(reshuffled) != before

    # Role and order are both carried, not just the ids.
    reroled = shot.model_copy(deep=True)
    reroled.citations[0] = AssetCitation(
        asset_id=reroled.citations[0].asset_id, role="first", order=reroled.citations[0].order
    )
    assert citation_fingerprint(reroled) != before
    renumbered = shot.model_copy(deep=True)
    renumbered.citations[0] = AssetCitation(
        asset_id=renumbered.citations[0].asset_id,
        role=renumbered.citations[0].role,
        order=renumbered.citations[0].order + 7,
    )
    assert citation_fingerprint(renumbered) != before

    # And a prompt change moves it not at all, which is the pairing that makes the guard mean
    # "this prose edit touched nothing else".
    reworded = shot.model_copy(deep=True)
    reworded.prompt = "Something else entirely."
    assert citation_fingerprint(reworded) == before


# --------------------------------------------------------------------------------------
# Half B — the route
# --------------------------------------------------------------------------------------


def test_the_report_writes_nothing_and_the_confirm_changes_only_the_prompt(tmp_path: Path):
    """The pass end to end on the Director's real plan, and the field-by-field guarantee.

    Two claims, and the second is the one the Director's timeline work depends on:

    * a report calls `store.save` on no path and says so on the wire (`project` is null);
    * after the confirmed call, **every field of every shot except `prompt` is equal to what it
      was**, compared through a fresh `ProjectStore` rather than through the handle that wrote.

    The comparison is a whole-model dump minus one key, not a list of fields someone remembered
    to check — a field added to `Shot` next month is covered by this test on the day it lands.
    """
    store = ProjectStore(tmp_path)
    project = director_project(store)
    rewrites = plain_rewrites(project)
    director = CleanupDirector(rewrites=rewrites)
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)
    before = {shot.id: shot.model_dump() for shot in project.shots}

    report = clean(client, project.id)
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["applied"] is False
    # The absence *is* the statement that nothing was written.
    assert body["project"] is None
    assert body["examined"] == 33
    assert body["echoing"] == DIRECTOR_ECHOING
    assert body["clean"] == 33 - DIRECTOR_ECHOING
    assert body["rewritten"] == DIRECTOR_ECHOING
    assert body["skipped"] == 0
    # Nothing on disk moved on the report path.
    assert {shot.id: shot.model_dump() for shot in store.get(project.id).shots} == before

    applied = clean(client, project.id, confirm_apply=True)
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    assert applied.json()["project"] is not None

    # Persistence through a *fresh* store, not the handle that wrote it.
    reread = ProjectStore(tmp_path).get(project.id)
    assert len(reread.shots) == 33
    for shot in reread.shots:
        was = dict(before[shot.id])
        now = shot.model_dump()
        # The rewritten prose where there was an echo, the untouched prose where there was not.
        assert now.pop("prompt") == rewrites.get(shot.id, was["prompt"])
        was.pop("prompt")
        # Every other field, whatever they are and however many there are.
        assert now == was
    assert comfy.prompts == []


def test_the_cleanup_cannot_move_a_window_or_change_the_shot_count(tmp_path: Path):
    """**The constraint that dominates this task**, asserted against the real geometry.

    The Director hand-tuned these 33 windows to musical timing and is happy with them:
    *"I have done some timeline touch ups and am generally happy for now."* Six of the cuts are
    deliberate micro-cuts no tiling function would ever produce. Undoing that work would be the
    worst outcome available to this feature, so it is pinned three ways: the whole fingerprint,
    the count, and the micro-cuts named individually.
    """
    store = ProjectStore(tmp_path)
    project = director_project(store)
    director = CleanupDirector(rewrites=plain_rewrites(project))
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)
    geometry = window_fingerprint(project)

    assert clean(client, project.id).status_code == 200
    assert window_fingerprint(store.get(project.id)) == geometry
    assert clean(client, project.id, confirm_apply=True).status_code == 200

    reread = ProjectStore(tmp_path).get(project.id)
    assert window_fingerprint(reread) == geometry
    assert len(reread.shots) == 33 == len(DIRECTOR_PLAN)
    # The manifest order itself, which is what the timeline numbers clips by.
    assert [shot.id for shot in reread.shots] == [row[0] for row in DIRECTOR_PLAN]
    # And the micro-cuts, named. These are the edges a repopulate would erase first.
    windows = {shot.id: (shot.start, shot.duration) for shot in reread.shots}
    assert windows["shot_1570bb6298ac"] == (107.5, 0.5)
    assert windows["shot_14ff133430ab"] == (61.0, 1.75)
    assert windows["shot_6026dc48c7ba"] == (62.75, 1.833)
    assert windows["shot_74c918882929"] == (64.583, 1.875)
    assert windows["shot_6a6137220f63"] == (21.333, 2.083)
    assert windows["shot_e17c546b7dae"] == (95.958, 2.667)
    assert comfy.prompts == []


def test_every_shots_citations_are_byte_identical_before_and_after(tmp_path: Path):
    """Citations are what make a prose edit safe, so a prose edit may not touch them.

    The asset ids are already on the shot, in roles, with orders — that is exactly why the
    sentence no longer has to carry the names, and it is the whole argument for this being a
    prose pass. A cleanup that dropped a citation would have removed the thing that justified
    it. Compared as an ordered tuple, so a reshuffle counts as a change.
    """
    store = ProjectStore(tmp_path)
    project = director_project(store)
    director = CleanupDirector(rewrites=plain_rewrites(project))
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)
    before = {shot.id: citation_fingerprint(shot) for shot in project.shots}
    # The plan really does have citations to preserve, or this test proves nothing.
    assert sum(len(value) for value in before.values()) >= 25

    assert clean(client, project.id, confirm_apply=True).status_code == 200
    reread = ProjectStore(tmp_path).get(project.id)
    assert {shot.id: citation_fingerprint(shot) for shot in reread.shots} == before
    # And the flat projection the render path reads.
    assert {shot.id: tuple(shot.asset_ids) for shot in reread.shots} == {
        shot.id: tuple(shot.asset_ids) for shot in project.shots
    }
    assert comfy.prompts == []


def test_a_harmless_mention_is_rephrased_and_not_mangled(tmp_path: Path):
    """The Director's own example of a *good* sentence with a label in it.

    "HarderFaster arches sensually on the Dusk Warehouse Bed" reads as a location, not as
    inventory. The fix is a phrase, not a new shot — so the route must accept a rewrite that
    keeps the performer, the action and the sentence's shape, and must not accept one that
    solves the problem by deleting the sentence.
    """
    store = ProjectStore(tmp_path)
    project = director_project(store)
    good = "HarderFaster arches sensually on the dusk-lit warehouse bed."
    director = CleanupDirector(
        rewrites={
            "shot_25b661593524": good,
            # The same shot's neighbour, answered with a gutting. Refused, reported, not
            # written — the sentence would have lost its direction along with the label.
            "shot_3133c0d92d6e": "High angle.",
        }
    )
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)

    body = clean(client, project.id, confirm_apply=True).json()
    rows = {row["shot_id"]: row for row in body["shots"]}

    kept = rows["shot_25b661593524"]
    assert kept["rewritten"] is True
    assert kept["before"] == "HarderFaster arches sensually on the Dusk Warehouse Bed."
    assert kept["after"] == good
    assert kept["labels"] == ["Dusk Warehouse Bed"]
    # The performer, the action and the adverb all survive; only the label is gone.
    assert "HarderFaster" in kept["after"] and "arches sensually" in kept["after"]

    gutted = rows["shot_3133c0d92d6e"]
    assert gutted["rewritten"] is False
    assert "deletion rather than a rephrasing" in gutted["reason"]

    reread = ProjectStore(tmp_path).get(project.id)
    stored = {shot.id: shot.prompt for shot in reread.shots}
    assert stored["shot_25b661593524"] == good
    assert stored["shot_3133c0d92d6e"] == "High angle view of HarderFaster on the Dusk Warehouse Bed."
    assert comfy.prompts == []


def test_a_prompt_with_no_echo_is_left_completely_alone_and_reported_as_such(
    tmp_path: Path,
):
    """A third of the Director's plan is already clean, and must stay untouched.

    Not sent to the model at all — the payload's selection is the guard — and reported anyway,
    because "nothing happened to this shot" has to say why.
    """
    store = ProjectStore(tmp_path)
    project = director_project(store)
    # A double that would rewrite *every* shot if the route let it near one.
    director = CleanupDirector(
        rewrites=lambda payload: {
            entry["shot_id"]: "REWRITTEN" for entry in payload["shots"]
        }
    )
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)
    already_clean = [
        shot.id for shot in project.shots if not echoed_labels(shot.prompt, project.assets)
    ]
    assert already_clean

    body = clean(client, project.id, confirm_apply=True).json()
    sent = {entry["shot_id"] for entry in director.calls[0]["input"]["shots"]}
    assert sent.isdisjoint(already_clean)

    rows = {row["shot_id"]: row for row in body["shots"]}
    reread = ProjectStore(tmp_path).get(project.id)
    stored = {shot.id: shot.prompt for shot in reread.shots}
    originals = {row[0]: row[3] for row in DIRECTOR_PLAN}
    for shot_id in already_clean:
        assert rows[shot_id]["rewritten"] is False
        assert rows[shot_id]["labels"] == []
        assert rows[shot_id]["after"] == ""
        assert "names no asset label" in rows[shot_id]["reason"]
        assert stored[shot_id] == originals[shot_id]
    assert comfy.prompts == []


def test_a_locked_shot_and_an_in_flight_render_are_skipped_in_the_existing_words(
    tmp_path: Path,
):
    """The two protections, and the wordings are the codebase's own rather than new ones.

    A lock is `EXPANSION_LOCKED_NOTICE` — the sentence expansion already refuses a prompt
    rewrite with, which is exactly the act being refused here. An in-flight render is
    `EXPANSION_RENDERED_NOTICE`, and only its in-flight arm is honoured: a job executing right
    now was submitted with this prompt, so rewriting it underneath would leave the record
    describing a submission that never happened.

    Neither shot is even sent to the model.
    """
    from music_video_producer.app import EXPANSION_LOCKED_NOTICE, EXPANSION_RENDERED_NOTICE

    store = ProjectStore(tmp_path)
    project = director_project(store)
    director = CleanupDirector(rewrites=lambda payload: dict(_strip_all(payload)))
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)
    by_id = {shot.id: shot for shot in project.shots}
    by_id["shot_aa9f610512f6"].locked = True
    by_id["shot_c8efd34a8333"].status = "running"
    store.save(project)

    body = clean(client, project.id, confirm_apply=True).json()
    sent = {entry["shot_id"] for entry in director.calls[0]["input"]["shots"]}
    assert "shot_aa9f610512f6" not in sent
    assert "shot_c8efd34a8333" not in sent

    rows = {row["shot_id"]: row for row in body["shots"]}
    assert rows["shot_aa9f610512f6"]["rewritten"] is False
    assert rows["shot_aa9f610512f6"]["reason"] == EXPANSION_LOCKED_NOTICE.format(
        shots=f"SHOT 05 ({'shot_aa9f610512f6'})"
    )
    assert rows["shot_c8efd34a8333"]["reason"] == EXPANSION_RENDERED_NOTICE.format(
        shots=f"SHOT 07 ({'shot_c8efd34a8333'})"
    )
    assert body["skipped"] == 2
    assert body["rewritten"] == DIRECTOR_ECHOING - 2

    reread = ProjectStore(tmp_path).get(project.id)
    stored = {shot.id: shot.prompt for shot in reread.shots}
    originals = {row[0]: row[3] for row in DIRECTOR_PLAN}
    assert stored["shot_aa9f610512f6"] == originals["shot_aa9f610512f6"]
    assert stored["shot_c8efd34a8333"] == originals["shot_c8efd34a8333"]
    # The protections themselves are untouched by the pass that respected them.
    assert {shot.id for shot in reread.shots if shot.locked} == {"shot_aa9f610512f6"}
    assert comfy.prompts == []


def test_a_rendered_or_approved_shot_is_rewritten_and_reported(tmp_path: Path):
    """The Director's ruling, carried across from citations to prose.

    *"So even with takes we do want the asset for the shot replaceable, that way a re-render
    would use the updated asset without losing previous takes."* (2026-08-20.) Prose is the
    looser coupling of the two: the prompt each take was submitted with is recorded on its job
    and in the take's own PNG metadata, so editing the shot's text loses no record.

    So they are rewritten — and counted, and named, before the confirm. The approval itself and
    the window it was made against are not written on any path.
    """
    store = ProjectStore(tmp_path)
    project = director_project(store)
    director = CleanupDirector(rewrites=plain_rewrites(project))
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)
    by_id = {shot.id: shot for shot in project.shots}
    rendered = by_id["shot_59f8da92c2d8"]
    rendered.status = "complete"
    rendered.latest_output = "out/take_00001.mp4"
    approved = by_id["shot_976ca97baa38"]
    approved.status = "approved"
    approved.approved_output = "out/take_00002.mp4"
    approved.approved_start = approved.start
    approved.approved_duration = approved.duration
    store.save(project)

    body = clean(client, project.id, confirm_apply=True).json()
    # They are *sent*, which is the half a report-shaped assertion cannot see: the model
    # selection is built from the protections, so a gate that refused a rendered shot would
    # simply leave it out of the payload and the row would still be filled by whatever the
    # double happened to answer. Found by mutation.
    sent = {entry["shot_id"] for entry in director.calls[0]["input"]["shots"]}
    assert {"shot_59f8da92c2d8", "shot_976ca97baa38"} <= sent
    assert body["rendered"] == 1
    assert body["approved"] == 1
    notes = " ".join(body["notes"])
    assert "carry an approved take" in notes
    assert "already hold a take that was rendered" in notes
    assert "SHOT 01 (shot_59f8da92c2d8)" in notes
    assert "SHOT 03 (shot_976ca97baa38)" in notes
    # And the honesty note the Director reads before applying.
    assert any("Read every rewrite before applying" in note for note in body["notes"])

    reread = ProjectStore(tmp_path).get(project.id)
    reread_by_id = {shot.id: shot for shot in reread.shots}
    assert "Gritty Warehouse Floor" not in reread_by_id["shot_59f8da92c2d8"].prompt
    assert "Gritty Warehouse Floor" not in reread_by_id["shot_976ca97baa38"].prompt
    # The take, the approval and the window it was made against, all exactly as they were.
    assert reread_by_id["shot_59f8da92c2d8"].latest_output == "out/take_00001.mp4"
    assert reread_by_id["shot_976ca97baa38"].approved_output == "out/take_00002.mp4"
    assert reread_by_id["shot_976ca97baa38"].approved_start == approved.start
    assert reread_by_id["shot_976ca97baa38"].approved_duration == approved.duration
    assert reread_by_id["shot_976ca97baa38"].status == "approved"
    assert comfy.prompts == []


def test_a_plan_with_no_echoes_refuses_rather_than_reporting_over_nothing(tmp_path: Path):
    """The honest-empty refusal, `snap_timeline_cuts`' rule.

    Nothing was examined that could change, so a 200 saying "0 rewritten" would read like the
    model was asked and had nothing to say. No model call is made at all.
    """
    from music_video_producer.app import CLEAN_PROMPTS_NOTHING_TO_CLEAN

    director = CleanupDirector(rewrites={"anything": "at all"})
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)
    for shot in project.shots:
        shot.prompt = "A performer alone in a moonlit room, singing to the lens."
    store.save(project)

    response = clean(client, project.id, confirm_apply=True)
    assert response.status_code == 422
    assert response.json()["detail"] == CLEAN_PROMPTS_NOTHING_TO_CLEAN
    assert director.calls == []
    assert comfy.prompts == []


def test_every_echoing_shot_being_protected_refuses_before_spending_a_model_call(
    tmp_path: Path,
):
    """The other honest empty: there is nothing to *send*.

    A state the Director fixes — unlock, or let the queue settle — rather than a report they
    act on, and refusing it costs no local-model time on a call with an empty shot list.
    """
    from music_video_producer.app import CLEAN_PROMPTS_ALL_PROTECTED

    director = CleanupDirector(rewrites={})
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)
    for shot in project.shots:
        if echoed_labels(shot.prompt, project.assets):
            shot.locked = True
    store.save(project)

    response = clean(client, project.id, confirm_apply=True)
    assert response.status_code == 422
    assert response.json()["detail"].startswith(CLEAN_PROMPTS_ALL_PROTECTED[:40])
    assert director.calls == []
    assert comfy.prompts == []


def test_an_unanswered_a_duplicated_and_a_stray_shot_are_reported_not_guessed_at(
    tmp_path: Path,
):
    """Three ways a local model answers badly, each reported and none of them written.

    A stray id is counted and never created into a shot — creating one would invent a window,
    which is the single thing this pass exists not to do.
    """
    store = ProjectStore(tmp_path)
    project = director_project(store)
    good = "Moonlight hits the cracked concrete floor in deep shadows."
    director = CleanupDirector(
        rewrites={"shot_59f8da92c2d8": good},
        extra=[
            # The same id again, with worse text. First answer wins.
            ("shot_59f8da92c2d8", "Moonlight. Floor. Shadows. Vibes."),
            ("shot_from_another_project", "Not about anything here."),
        ],
    )
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)

    body = clean(client, project.id, confirm_apply=True).json()
    rows = {row["shot_id"]: row for row in body["shots"]}
    assert rows["shot_59f8da92c2d8"]["after"] == good
    assert "addressed no shot in this project" in body["message"]
    # Every other echoing shot went unanswered and is named as such, not silently dropped.
    assert rows["shot_320c841cdad0"]["rewritten"] is False
    assert rows["shot_320c841cdad0"]["reason"] == "the model returned no rewrite for this shot"

    reread = ProjectStore(tmp_path).get(project.id)
    assert len(reread.shots) == 33
    assert {shot.id for shot in reread.shots} == {row[0] for row in DIRECTOR_PLAN}
    stored = {shot.id: shot.prompt for shot in reread.shots}
    assert stored["shot_59f8da92c2d8"] == good
    assert stored["shot_320c841cdad0"] == DIRECTOR_PLAN[1][3]
    assert comfy.prompts == []


def test_the_pass_selects_its_persona_and_tells_the_model_which_labels_to_lose(
    tmp_path: Path,
):
    """What is actually sent, since no live model has ever validated it.

    The system prompt is the cleanup persona and not the story pass's, the payload carries the
    labels per shot, and nothing about a window travels. This is the contract a live run will
    be judged against, so it is pinned rather than assumed.
    """
    from music_video_producer.prompt_cleanup import PROMPT_CLEANUP_SYSTEM_PROMPT

    store = ProjectStore(tmp_path)
    project = director_project(store)
    director = CleanupDirector(rewrites=plain_rewrites(project))
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)

    assert clean(client, project.id).status_code == 200
    call = director.calls[0]
    assert call["system_prompt"] == PROMPT_CLEANUP_SYSTEM_PROMPT
    assert "Performer names are NOT labels and must stay" in call["system_prompt"]
    entries = {entry["shot_id"]: entry for entry in call["input"]["shots"]}
    assert entries["shot_6fa420d811aa"]["labels"] == [
        "Dusk Warehouse Bed",
        "Blue Haze Atmosphere",
    ]
    assert entries["shot_6fa420d811aa"]["section"]["label"] == "Chorus 2"
    assert comfy.prompts == []


def test_the_geometry_guard_refuses_without_saving_when_a_window_would_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The last line of defence, forced to fire.

    It is unreachable by construction — the write loop assigns `Shot.prompt` and nothing else —
    which is exactly why it is a check on data rather than a claim about code, and exactly why
    it needs a test that does not depend on the code being wrong. The fingerprint function is
    stood in for by one that reports a move; the route must then refuse **and leave the
    manifest untouched**, prose included.

    If this branch ever fires in the field it means something upstream started writing windows
    on this path, and the Director's timeline survives finding that out.
    """
    from music_video_producer import app as app_module
    from music_video_producer.app import CLEAN_PROMPTS_WINDOWS_MOVED

    store = ProjectStore(tmp_path)
    project = director_project(store)
    director = CleanupDirector(rewrites=plain_rewrites(project))
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)
    before = {shot.id: shot.model_dump() for shot in project.shots}

    calls = {"n": 0}

    def drifting(project):
        calls["n"] += 1
        return (calls["n"],)

    monkeypatch.setattr(app_module, "window_fingerprint", drifting)

    response = clean(client, project.id, confirm_apply=True)
    assert response.status_code == 500
    assert response.json()["detail"] == CLEAN_PROMPTS_WINDOWS_MOVED
    # Nothing was saved — not the windows it refused over, and not the prose either.
    assert {shot.id: shot.model_dump() for shot in ProjectStore(tmp_path).get(project.id).shots} == before
    assert comfy.prompts == []


def test_the_citation_guard_refuses_without_saving_when_a_citation_would_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The same guard's other half, and the reason it is checked at all.

    Citations are what make a prose edit safe: the asset ids are already on the shot, so the
    sentence does not have to carry the names. A pass that dropped one would have removed its
    own justification, so it refuses rather than saving a plan whose references had shifted
    under a wording change.
    """
    from music_video_producer import app as app_module
    from music_video_producer.app import CLEAN_PROMPTS_WINDOWS_MOVED

    store = ProjectStore(tmp_path)
    project = director_project(store)
    director = CleanupDirector(rewrites=plain_rewrites(project))
    client, store, comfy = make_client(tmp_path, director)
    project = director_project(store)
    before = {shot.id: shot.model_dump() for shot in project.shots}

    calls = {"n": 0}

    def drifting(shot):
        calls["n"] += 1
        return (calls["n"],)

    monkeypatch.setattr(app_module, "citation_fingerprint", drifting)

    response = clean(client, project.id, confirm_apply=True)
    assert response.status_code == 500
    assert response.json()["detail"] == CLEAN_PROMPTS_WINDOWS_MOVED
    assert {shot.id: shot.model_dump() for shot in ProjectStore(tmp_path).get(project.id).shots} == before
    assert comfy.prompts == []


def test_the_directors_live_project_is_never_written_by_this_test_module():
    """The manifest under `data/` is evidence, not a fixture. Read, never touched.

    Every test above rebuilds the plan in `tmp_path`. This asserts the real file is still there
    and still holds the 33 shots and 24 echoes the whole feature was measured against, so a
    test that accidentally reached for the live store would be caught here rather than by the
    Director noticing their timeline had changed.
    """
    if not LIVE_PROJECT.exists():
        pytest.skip("the Director's live project is not present in this checkout")
    manifest = json.loads(LIVE_PROJECT.read_text(encoding="utf-8"))
    assert len(manifest["shots"]) == 33
    library = [
        Asset(id=item["id"], name=item["name"], kind=item["kind"], path=item["path"])
        for item in manifest["assets"]
    ]
    echoing = [
        shot for shot in manifest["shots"] if echoed_labels(shot["prompt"], library)
    ]
    assert len(echoing) == DIRECTOR_ECHOING
    # `DIRECTOR_PLAN` rounds to milliseconds for readability; the manifest holds the raw floats
    # the drag produced. The identity that matters is the shot list and its geometry to the
    # millisecond, which is finer than any edit this feature could make.
    assert [
        (shot["id"], round(shot["start"], 3), round(shot["duration"], 3))
        for shot in manifest["shots"]
    ] == [(shot_id, start, duration) for shot_id, start, duration, _ in DIRECTOR_PLAN]
