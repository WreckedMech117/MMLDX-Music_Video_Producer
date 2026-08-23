"""The three steps of a populate — lay it out, line it up, fill it in.

The Director's model (2026-08-21): *"Populating the timeline is definitely a multi-step
process.. Laying it out, lining it up, filling it in."* This suite is about the split itself,
and its headline is the one property the split had to keep: **a chained populate produces
exactly what the single route produced.**

Nothing here calls a model, starts ComfyUI, submits a render or touches the Director's live
project. `comfy.prompts` is asserted empty on every path.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from itertools import pairwise
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from music_video_producer import app as app_module
from music_video_producer import timeline as timeline_module
from music_video_producer.app import (
    FILL_IN_NO_PLAN,
    FILL_IN_PLAN_MISMATCH,
    FILL_IN_WINDOWS_MOVED,
    LAY_OUT_NO_PLAN,
    LAY_OUT_PLAN_MISMATCH,
    LINE_UP_NO_PLAN,
    LINE_UP_WINDOWS_CHANGED,
    POPULATE_CONFIRM_REFUSAL,
    POPULATE_MAX_WINDOW_SECONDS,
    PROJECT_CHANGED_REFUSAL,
    create_app,
)
from music_video_producer.batch import window_band_note
from music_video_producer.config import Settings
from music_video_producer.models import (
    Asset,
    Project,
    RenderJob,
    Shot,
    Song,
    SongSection,
)
from music_video_producer.store import ProjectStore
from music_video_producer.timeline import (
    POPULATE_VARIANCE_DEFAULT,
    POPULATE_VARIANCE_MAX,
    SNAP_CLEARANCE_SECONDS,
    SNAP_CONTIGUITY_TOLERANCE,
    SNAP_LOCKED_REFUSAL,
    WINDOW_LAY_RESOLUTION,
    snap_cut_plan,
    vocal_gaps,
)

# --------------------------------------------------------------------------------------------
# The realistic case, built here rather than copied from the Director's data root.
#
# It is the *shape* of their live project — a 154.6 s song with measured voice and a silent
# intro and outro, seven marked sections, a declared location, a character whose multiview sheet
# has been promoted, and proposals that name assets both structurally and in their prose — with
# none of its churn. The live project is read by `test_api.py`'s own pin and is edited by the
# Director between sessions; a byte-identity digest pinned to it would fail for their edits
# rather than for a regression.
# --------------------------------------------------------------------------------------------

SONG_DURATION = 154.644898
#: Whisper's measured voice on the real track: an instrumental intro, a gap, and a long silent
#: outro. The intro and outro are what make `fill_in_shots`' voiceless downgrade fire.
VOCAL_SPANS = [[11.0, 87.72], [89.14, 99.2], [103.2, 120.1], [123.2, 134.56]]

#: Where words were *heard*, for the Phase D variance driver — timings only, never a lyric.
#:
#: One "word" every 0.30 s to 0.75 s inside each measured vocal span, on a four-step cycle
#: offset per span, so the onset rate rises and falls across every section the way a sung line
#: does. Deterministic to the millisecond: the byte digests below are pinned through it.
def lyric_word_times() -> list[tuple[str, float, float]]:
    words: list[tuple[str, float, float]] = []
    for index, (low, high) in enumerate(VOCAL_SPANS):
        at, step = low, 0
        while at + 0.2 <= high:
            words.append(("la", round(at, 2), round(at + 0.2, 2)))
            at += 0.30 + 0.15 * ((step + index) % 4)
            step += 1
    return words


SECTIONS = [
    ("Intro", 0.0, 11.0, "Empty, moonlit warehouse."),
    ("Verse", 11.0, 21.54, "Vast empty floor; handheld and low."),
    ("Chorus", 32.54, 23.82, "Black draped canopy bed; slow glides."),
    ("Verse 2", 56.36, 20.84, "Vast empty floor; handheld and low."),
    ("Chorus 2", 77.2, 26.0, "Black draped canopy bed; slow glides."),
    ("Bridge", 103.2, 20.9, "Flickering, erratic light."),
    ("Outro", 124.1, 30.54, "Cold moonlight; haunting stillness."),
]

ASSETS = [
    ("asset_setting", "Dusk Warehouse Bed", "setting", "upload", None),
    ("asset_source", "HarderFaster", "character", "upload", None),
    # Promoted from the source above, which is what makes `citable_assets` hide it and
    # `prefer_identity_sheets` substitute it — both no-ops on a project without one.
    ("asset_sheet", "HarderFaster · multiview", "character", "krea-multiview", "asset_source"),
    ("asset_haze", "Blue Haze Atmosphere", "style", "stage-manager", None),
    ("asset_lips", "Crimson Lips Close-up", "prop", "stage-manager", None),
    ("asset_mic", "Vintage Chrome Mic", "prop", "stage-manager", None),
]


def realistic_project(
    store: ProjectStore, *, sections: bool = True, words: bool = False
) -> str:
    """The project every test here populates. Deterministic down to the ids.

    `words` adds Whisper's word *times*, which is what Phase D's variance driver reads. Off by
    default so every test written before it keeps the song it was written against — and because
    a song with no word times is itself a case worth having: the driver is absent, so the layout
    is the one this application laid before variance existed, whatever the parameter says.
    """
    project = Project(
        id="project_threestep",
        name="Harder Faster",
        treatment="A performer alone in a warehouse at night.",
        style_bible="Sodium amber and deep blacks.",
        default_setting_id="asset_setting",
        song=Song(
            title="Harder Faster",
            source="imported",
            path="media/song.flac",
            duration=SONG_DURATION,
            lyrics="[Intro]\n\n[Verse]\nHarder faster\n",
            vocal_spans=VOCAL_SPANS,
            lyric_words=lyric_word_times() if words else [],
        ),
        sections=(
            [
                SongSection(
                    id=f"section_{index}",
                    label=label,
                    start=start,
                    duration=length,
                    prompt=prompt,
                )
                for index, (label, start, length, prompt) in enumerate(SECTIONS)
            ]
            if sections
            else []
        ),
        assets=[
            Asset(
                id=asset_id,
                name=name,
                kind=kind,
                source=source,
                parent_id=parent,
                path=f"media/assets/{asset_id}.png",
            )
            for asset_id, name, kind, source, parent in ASSETS
        ],
    )
    store.save(project)
    return project.id


#: 34 proposals for a song that needs 30 — the live plan's own count, so the tiling repair and
#: `proposal_for_position` both run against more proposals than windows. Every fourth shot
#: declares its assets structurally, every other names one in the prose (the demoted scan), and
#: the rest cite nothing at all.
def proposals(tag: str) -> list[dict]:
    rows = []
    cursor = 0.0
    for index in range(34):
        length = 4.0 + (index % 5)
        row = {
            "start": round(cursor, 3),
            "duration": length,
            "prompt": f"[{tag}] Shot {index}: the performer against the dark.",
            "performance": index % 3 != 0,
            "assets": [],
        }
        if index % 4 == 0:
            row["assets"] = ["HarderFaster", "Blue Haze Atmosphere"]
        elif index % 2 == 1:
            row["prompt"] += " Crimson Lips Close-up fills the frame."
        rows.append(row)
        cursor += length
    return rows


def section_rows(tag: str) -> list[dict]:
    return [
        {"label": label, "start": start, "duration": length, "prompt": f"[{tag}] {prompt}"}
        for label, start, length, prompt in SECTIONS
    ]


def _obj(name: str, **fields):
    return type(name, (), fields)()


class StepDirector:
    """A recording plan double that answers **differently on every call**.

    The reply carries its own call index in every prompt and every section look, so a second,
    unnoticed ask cannot hide behind a deterministic double: if a stage asked twice, the text
    that landed would say so.
    """

    def __init__(self, script: list[tuple[list[dict], list[dict]]] | None = None):
        self.script = script
        self.requests: list[dict] = []

    async def plan(self, *, message, project_context, temperature=None, response_schema=None):
        index = len(self.requests)
        self.requests.append(
            {
                "message": message,
                "temperature": temperature,
                "response_schema": response_schema,
                "context": project_context,
            }
        )
        if self.script is not None:
            shots, sections = self.script[min(index, len(self.script) - 1)]
        else:
            shots, sections = proposals(f"c{index}"), section_rows(f"c{index}")
        return _obj(
            "DirectorResult",
            message=f"Laid out on call {index}.",
            treatment="",
            style_bible="",
            shots=[
                _obj(
                    "PlannedShot",
                    start=row["start"],
                    duration=row["duration"],
                    prompt=row["prompt"],
                    performance=row["performance"],
                    assets=list(row["assets"]),
                )
                for row in shots
            ],
            sections=[
                _obj(
                    "PlannedSection",
                    label=row["label"],
                    start=row["start"],
                    duration=row["duration"],
                    prompt=row["prompt"],
                )
                for row in sections
            ],
        )


class NullComfy:
    """Never rendered against. `prompts` is the assertion surface every test uses."""

    def __init__(self) -> None:
        self.prompts: list = []
        self.uploads: list = []


def make_client(
    tmp_path: Path, director=None, *, sections: bool = True, words: bool = False
):
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    comfy = NullComfy()
    director = director or StepDirector()
    app = create_app(settings=settings, store=store, comfy=comfy, director=director)
    return (
        TestClient(app),
        store,
        comfy,
        director,
        realistic_project(store, sections=sections, words=words),
    )


def plan_digest(project: dict) -> str:
    """Every field a populate writes, with the one nondeterministic identity normalised.

    Shot ids are minted fresh on every run, so they are dropped; **nothing else is**. Every
    other field of every shot rides into the digest, which is what makes this a statement about
    citations, prompts, `singing`, `use_song_audio`, seeds and windows together rather than
    about the four counts a response happens to report.
    """
    payload = {
        "sections": [
            {key: value for key, value in section.items() if key != "id"}
            for section in project.get("sections", [])
        ],
        "shots": [
            {key: value for key, value in shot.items() if key != "id"}
            for shot in project.get("shots", [])
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def populate(client: TestClient, project_id: str, **body) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/timeline/populate",
        json={"confirm_replace": True, **body},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------------------------
# The headline: byte identity.
# --------------------------------------------------------------------------------------------

#: Originally measured on `master` at 9c3db45 — the single-route populate, before the split
#: existed — by running this test's body against a stashed tree. Both arms are here because the
#: two take different branches: the marked sections tile per section, the unmarked ones tile the
#: whole song as one span and then adopt whatever structure the shots call volunteered.
#:
#: **Phase B reached them through `snap_tolerance=0`** and did not re-measure them. Phase B
#: moves windows onto phrase boundaries, so there is no byte pin on a *default* populate any
#: more — the plan it produces is a different plan, which is the point of the phase. What
#: survived untouched was the control arm: snapping switched off left lay-out and fill-in
#: producing the very bytes they produced before line-up could move anything.
#:
#: **Both were re-measured on 2026-08-22 and neither is a pre-split pin any longer.** The
#: per-section proposal pairing (`app.paired_proposals`) deliberately changes which prompt a
#: window carries, which is the whole of the defect it closes, so a digest that did not move
#: would have meant the fix did nothing. The pre-2026-08-22 values are recorded here rather than
#: deleted, because the claim they carried is worth being able to reconstruct:
#:
#:   marked   8b101523562766bba9abb858498aa79b75f536ffe2f6ff771b59a11b60cab0b8
#:   unmarked 085917f16a42fd9116ba31e8e34952c0c763e36c8d046879787bb6035e3c88c5
#:
#: Note that **both** arms moved, and the second is the more interesting one: "no sections" here
#: means the *Director* marked none, not that the plan has none — `lay_out_shots` adopts the
#: structure the shots call volunteered, so that arm tiles per section too. The genuinely
#: section-free path (`timeline.layout_spans` answering `[]`) is byte-identical to the old
#: global rule and is pinned on its own by
#: `test_a_layout_with_no_sections_at_all_still_maps_by_global_proportion`.
#:
#: What these two now pin is that the pairing, the tiling and everything fill-in writes are
#: deterministic for a given model reply — the property the split was built for — not that they
#: agree with a populate that predates the fix.
#:
#: **Re-measured again on 2026-08-23** when the Director ruled `POPULATE_MAX_WINDOW_SECONDS` from
#: 6.0 to 6.8. A wider band is a different tiling, so both had to move; the values they replace
#: are kept beside them, as the pair above is:
#:
#:   marked   74af40ecc06c295923c4aef2517d9d22f1d2e5c1b4a0aee7584b7dd03194ede6
#:   unmarked 4accf454c1424d4fbd6d3a77ba41aecf95cf66120af5e759990d8549d3ab914c
#:
#: **And the move is the ceiling's and nothing else's, checked rather than assumed.** Coverage is
#: 0 → 154.640 on both, exactly what it was; the worst seam disagreement is 1 ms, inside
#: `assembly.BOUNDARY_TOLERANCE_SECONDS` (20.8 ms); every window is ≥ 4.000 and ≤ 6.800;
#: `batch.window_band_note` raises **0** warnings and `assembly.tiling_refusals` **0** refusals,
#: both of which were 0 before. The count goes **29 → 27**, and only two spans lose a window,
#: each because `ceil(span / ceiling)` — `populate_windows`' own lower clamp — drops by one:
#:
#:   Chorus 2  26.00 s  ceil(26.00/6.0)=5 → ceil(26.00/6.8)=4   4 windows of 6.500 s
#:   Outro     30.54 s  ceil(30.54/6.0)=6 → ceil(30.54/6.8)=5   5 windows of 6.108 s
#:
#: The other five spans are bounded by their proposal count or by `floor(span/4)`, neither of
#: which the ceiling touches, and they keep the counts they had. The band goes 4.134–6.000 to
#: 4.000–6.800.
#:
#: **Re-measured once more on 2026-08-23** when `timeline.layout_spans` stopped dropping the
#: unmarked stretch at the end of the song. The values they replace are kept beside them, as both
#: pairs above are:
#:
#:   marked   ad0f82d0f1e5a84dcb56207e92a4f121b658489329cbbd8cd7918b1c75f53bca
#:   unmarked 0567041f3746e287411436730e314d9fa8209bf98a506846e6277a245767a434
#:
#: **And the move is the trailing stub's and nothing else's, checked shot by shot rather than
#: assumed.** This fixture's last section ends at 154.640 and the song is 154.644898, so 4.9 ms
#: of it had no span; that stub is now absorbed into the Outro span, which is the only span whose
#: length changes. Diffing the two plans field by field, **5 of 27 shots differ and every
#: difference is geometry** — shots 22–26, the Outro's five windows, each moved by at most 1 ms;
#: no prompt, seed, mode, citation, `singing` or `use_song_audio` moves on any shot, and the
#: unmarked arm shows the identical five. Coverage goes 0 → 154.640 to **0 → 154.644**, which is
#: the song to within the millisecond each span's last window is floored to. Everything else is
#: unchanged: 27 shots, band 4.000–6.800, worst seam 1 ms, `batch.window_band_note` **0**
#: warnings, `assembly.tiling_refusals` **0** refusals.
CHAINED_DIGEST_SECTIONS_MARKED = (
    "e84a8587382283b70dbaa0399a744f8def29a36fcacfb9a4ca288492e895006e"
)
CHAINED_DIGEST_NO_SECTIONS = (
    "1607b49e3f452dc39aa15138137ebfad2b23021bf1744d5dac8051b3c41114d0"
)


@pytest.mark.parametrize("words", [False, True], ids=["no-word-times", "word-timed"])
@pytest.mark.parametrize(
    "marked,expected",
    [
        (True, CHAINED_DIGEST_SECTIONS_MARKED),
        (False, CHAINED_DIGEST_NO_SECTIONS),
    ],
)
def test_a_chained_populate_with_snapping_off_writes_what_the_single_route_wrote(
    tmp_path: Path, marked: bool, expected: str, words: bool
):
    """**Determinism, to the byte.** Same shots, same windows, same citations, same prompts,
    same `singing`, same seeds, given the same model reply.

    Everything a populate writes is inside the digest except the shot ids, which are minted
    fresh on every run and could not be identical if they tried.

    Reached with `snap_tolerance=0` since Phase B: tolerance 0 is snapping switched off and a
    genuine no-op, so line-up contributes nothing here and what is pinned is lay-out's tiling
    and fill-in's content. **Re-measured 2026-08-22** when the per-section pairing changed which
    proposal a window takes — see the constants above for both values and for why the older
    pair is no longer the right claim.

    **`variance=0` joined it on 2026-08-23, and the `word-timed` arm is what makes that a
    claim.** Phase D reshapes a section's windows around how busy the singing is, so the neutral
    arm has to be run against a song that *has* word times — otherwise "the digest did not move"
    would only say the driver was missing. Both arms are here: with no word times the layout is
    the old one whatever the parameter says, and with them it is the old one because the
    parameter is 0. The default is pinned separately, and to a different digest.
    """
    client, _store, comfy, _director, project_id = make_client(
        tmp_path, sections=marked, words=words
    )
    body = populate(client, project_id, snap_tolerance=0, variance=0)

    assert body["proposed"] == 34
    assert body["moved"] == 0 and body["skipped"] == 0, "tolerance 0 examined a cut"
    assert plan_digest(body["project"]) == expected
    # And what persisted, read back through a store that shares nothing with the app's.
    assert plan_digest(ProjectStore(tmp_path).get(project_id).model_dump(mode="json")) == expected
    assert comfy.prompts == []


def test_a_chained_populate_asks_the_model_exactly_once(tmp_path: Path):
    """One call for all three steps — the split may not buy itself a second ask.

    The double answers differently on every call (`StepDirector`), so this is not merely a
    count: the prompts that landed are checked to be the *first* answer's, which a second ask
    would have overwritten with `[c1]`.
    """
    client, _store, comfy, director, project_id = make_client(tmp_path)
    body = populate(client, project_id)

    assert len(director.requests) == 1
    assert all(shot["prompt"].startswith("[c0]") for shot in body["project"]["shots"])
    assert comfy.prompts == []


def test_the_chain_calls_the_same_three_steps_the_routes_call(tmp_path: Path, monkeypatch):
    """The chain is a caller, not a fourth implementation — asserted, not assumed.

    Each step is monkeypatched at the module level the routes resolve it from. If the chain
    held its own copy of any of them the substitution would go unseen, which is precisely how
    this codebase has twice ended up with two implementations of one rule.
    """
    seen: list[str] = []
    lay_out, line_up, fill_in = (
        app_module.lay_out_shots,
        app_module.line_up_shots,
        app_module.fill_in_shots,
    )

    async def spy_lay_out(*args, **kwargs):
        seen.append("lay-out")
        return await lay_out(*args, **kwargs)

    def spy_line_up(*args, **kwargs):
        seen.append("line-up")
        return line_up(*args, **kwargs)

    def spy_fill_in(*args, **kwargs):
        seen.append("fill-in")
        return fill_in(*args, **kwargs)

    monkeypatch.setattr(app_module, "lay_out_shots", spy_lay_out)
    monkeypatch.setattr(app_module, "line_up_shots", spy_line_up)
    monkeypatch.setattr(app_module, "fill_in_shots", spy_fill_in)

    client, _store, comfy, _director, project_id = make_client(tmp_path)
    populate(client, project_id)

    assert seen == ["lay-out", "line-up", "fill-in"]
    assert comfy.prompts == []


# --------------------------------------------------------------------------------------------
# Each step on its own route.
# --------------------------------------------------------------------------------------------


def lay_out(client: TestClient, project_id: str, **body) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/timeline/lay-out", json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


def line_up(client: TestClient, project_id: str, plan: dict | None = None, **body) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/timeline/line-up", json={"plan": plan, **body}
    )
    assert response.status_code == 200, response.text
    return response.json()


def fill_in(client: TestClient, project_id: str, plan: dict, **body) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/timeline/fill-in", json={"plan": plan, **body}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_each_step_reports_without_writing_and_the_confirm_persists(tmp_path: Path):
    """Report first, apply on confirm — through a *fresh* `ProjectStore` on every claim.

    Lay-out's report asks the model and writes nothing; its confirm writes windows and only
    windows. Line-up never writes at all. Fill-in's report shows the content and writes
    nothing; its confirm lands it.

    **`words=True` since 2026-08-23**, because the line-up half of this test asserts `moved > 0`
    and needs a cut that can actually move. The word-timed arm is also the production path:
    `vocal_gaps` has read `Song.lyric_words` since Phase B and the Director's song carries 262 of
    them, where the `words=False` arm falls back to `merge_vocal_spans`' *display* view. On that
    fallback the whole 154.6 s song offers five gaps, only one of them interior to a sung
    stretch, and at the 6.8 s ceiling the single cut in reach of it is refused by the band — see
    `test_a_chained_populate_moves_its_cuts_onto_phrase_boundaries` for the measurement. Nothing
    else in this test depends on the arm.
    """
    client, _store, comfy, _director, project_id = make_client(tmp_path, words=True)
    # The revision is the proof, not the shot count: a report that saved an unchanged project
    # would leave no shots behind either, and would still have written.
    untouched = ProjectStore(tmp_path).get(project_id).updated_at

    layout = lay_out(client, project_id)
    assert layout["applied"] is False and layout["project"] is None
    assert layout["required"] == 30 and layout["proposed"] == 34
    assert len(layout["proposals"]) == 34 and len(layout["windows"]) == layout["created"]
    assert layout["sections_origin"] == "director", "the Director's own boxes were reported"
    assert ProjectStore(tmp_path).get(project_id).shots == [], "the report wrote shots"
    assert (
        ProjectStore(tmp_path).get(project_id).updated_at == untouched
    ), "the report saved the project"

    applied = client.post(
        f"/api/projects/{project_id}/timeline/lay-out",
        json={"confirm_replace": True, "plan": layout},
    )
    assert applied.status_code == 200, applied.text
    layout_applied = applied.json()
    assert layout_applied["applied"] is True
    persisted = ProjectStore(tmp_path).get(project_id)
    assert len(persisted.shots) == len(layout["windows"])
    # The Director's own boxes are left exactly as they are — same ids, same prompts. Only a
    # section layer this step laid out is written, or the confirm would mint new ids for boxes
    # nobody changed and quietly orphan anything that names one.
    assert [section.id for section in persisted.sections] == [
        f"section_{index}" for index in range(len(SECTIONS))
    ]
    assert [section.prompt for section in persisted.sections] == [
        prompt for *_, prompt in SECTIONS
    ]
    # Structure and only structure: no content decision was taken by this step.
    assert [shot.prompt for shot in persisted.shots] == [""] * len(persisted.shots)
    assert all(not shot.citations and shot.seed == 0 for shot in persisted.shots)
    assert all(shot.singing == "unknown" for shot in persisted.shots)

    revision = ProjectStore(tmp_path).get(project_id).updated_at
    # Line-up's own report: it proposes the moves and writes nothing until it is confirmed.
    aligned = line_up(client, project_id, layout_applied)
    assert aligned["applied"] is False and aligned["project"] is None
    assert aligned["measured"] is True and aligned["moved"] > 0
    assert (
        ProjectStore(tmp_path).get(project_id).updated_at == revision
    ), "line-up's report saved the project"
    assert [shot.start for shot in ProjectStore(tmp_path).get(project_id).shots] == [
        row["start"] for row in layout_applied["windows"]
    ], "line-up's report moved a window on disk"

    aligned = line_up(client, project_id, layout_applied, confirm_apply=True)
    assert aligned["applied"] is True
    persisted = ProjectStore(tmp_path).get(project_id)
    assert [shot.start for shot in persisted.shots] == [
        row["start"] for row in aligned["windows"]
    ], "the confirm did not land the windows it reported"
    assert [shot.prompt for shot in persisted.shots] == [""] * len(persisted.shots), (
        "line-up wrote content"
    )
    revision = persisted.updated_at

    report = fill_in(client, project_id, aligned)
    assert report["applied"] is False and report["project"] is None
    assert report["filled"] == len(persisted.shots)
    assert ProjectStore(tmp_path).get(project_id).updated_at == revision
    assert all(
        shot.prompt == "" for shot in ProjectStore(tmp_path).get(project_id).shots
    ), "the report wrote content"

    written = fill_in(client, project_id, aligned, confirm_apply=True)
    assert written["applied"] is True
    fresh = ProjectStore(tmp_path).get(project_id)
    assert all(shot.prompt.startswith("[c0]") for shot in fresh.shots)
    assert all(shot.use_song_audio for shot in fresh.shots)
    assert [shot.seed for shot in fresh.shots] == list(range(1, len(fresh.shots) + 1))
    assert any(shot.citations for shot in fresh.shots)
    assert comfy.prompts == []


def test_the_three_steps_by_hand_produce_what_the_chain_produces(tmp_path: Path):
    """The routes and the chain are the same three functions, so they land the same plan.

    Windows, prompts, citations, `singing`, `use_song_audio` and seeds all compared — the same
    digest the byte-identity test takes, minus the section layer, which the hand-run sequence
    writes at the lay-out step and the chain writes at the save.
    """
    stepped_client, _stepped_store, _, _, stepped_id = make_client(tmp_path / "stepped")
    layout = lay_out(stepped_client, stepped_id)
    layout = stepped_client.post(
        f"/api/projects/{stepped_id}/timeline/lay-out",
        json={"confirm_replace": True, "plan": layout},
    ).json()
    aligned = line_up(stepped_client, stepped_id, layout, confirm_apply=True)
    fill_in(stepped_client, stepped_id, aligned, confirm_apply=True)

    chained_client, _, _, _, chained_id = make_client(tmp_path / "chained")
    populate(chained_client, chained_id)

    stepped = ProjectStore(tmp_path / "stepped").get(stepped_id)
    chained = ProjectStore(tmp_path / "chained").get(chained_id)
    assert [
        (shot.start, shot.duration, shot.prompt, shot.singing, shot.seed, shot.use_song_audio,
         [(c.asset_id, c.role, c.order) for c in shot.citations])
        for shot in stepped.shots
    ] == [
        (shot.start, shot.duration, shot.prompt, shot.singing, shot.seed, shot.use_song_audio,
         [(c.asset_id, c.role, c.order) for c in shot.citations])
        for shot in chained.shots
    ]


def test_line_up_measures_every_window(tmp_path: Path):
    """Step two attaches the musical facts to every window it reports.

    The intro and the outro of this song are measured voiceless, which is the one fact fill-in
    consumes — a window the track leaves silent cannot be marked singing whatever the model
    declared.
    """
    client, _store, comfy, _director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    aligned = line_up(client, project_id, layout)

    assert len(aligned["windows"]) == len(layout["windows"])
    assert all(row["vocal_seconds"] is not None for row in aligned["windows"])
    assert aligned["windows"][0]["voiceless"] is True, "the silent intro reads as voiced"
    assert aligned["windows"][-1]["voiceless"] is True, "the silent outro reads as voiced"
    assert any(not row["voiceless"] for row in aligned["windows"])
    assert {row["section"] for row in aligned["windows"]} == {
        label for label, *_ in SECTIONS
    }
    assert ProjectStore(tmp_path).get(project_id).shots == []
    assert comfy.prompts == []


def test_an_unmeasured_song_lines_up_as_unmeasured_rather_than_silent(tmp_path: Path):
    """`None` is unmeasured, not silent — the absent-analysis convention, on this wire too."""
    client, store, comfy, _director, project_id = make_client(tmp_path)
    project = store.get(project_id)
    project.song.vocal_spans = []
    store.save(project)

    aligned = line_up(client, project_id, lay_out(client, project_id))
    assert aligned["measured"] is False
    assert all(row["vocal_seconds"] is None for row in aligned["windows"])
    assert all(row["voiceless"] is False for row in aligned["windows"])
    assert comfy.prompts == []


# --------------------------------------------------------------------------------------------
# Phase B — line-up consumes the alignment.
#
# Two claims and they are separable: the windows move onto the music through **one** snapping
# implementation reached by two doors, and every window is handed the lyric lines sung across it
# with the singer slots the sheet marks on them. Nothing here chooses a citation from a singer —
# that is fill-in's, deliberately unbuilt.
#
# The song below is synthetic on purpose. Where the voice is and is not has to be arithmetic a
# reader can check in their head for a test about cut placement to mean anything; the Director's
# real track is measured live and written up in the development log, not asserted here.
# --------------------------------------------------------------------------------------------

#: Six sung phrases of eight seconds with a two-second rest between them, on a sixty second
#: song. Every interior gap is 2.1 s — from the last word's end at x7.9 to the next phrase at
#: x0.0 — which is wide enough to hold `SNAP_CLEARANCE_SECONDS` at both ends with room to spare.
PHRASE_SONG_DURATION = 60.0
PHRASE_WINDOWS = tuple((float(index * 5), 5.0) for index in range(12))


def heard_word(block: int, index: int) -> str:
    """A distinct two-letter word per position — `"aa"`, `"ab"` … `"fh"`.

    Alphabetic and short, both deliberately. `timeline._lyric_tokens` reads `[a-z]+`, so a
    token carrying a digit tokenises to its letters alone and every "word12" in a fixture
    becomes the same token `"word"` — which makes a whole transcript match a whole sheet and
    quietly turns a line-alignment test into a test of nothing. Two letters is also below
    `_tokens_agree`'s four-letter prefix rule, so these match only their exact selves.
    """
    return f"{chr(97 + block % 26)}{chr(97 + index % 26)}"


def phrase_words() -> list[tuple[str, float, float]]:
    """The transcript. The first phrase ends with the words the chorus opens on.

    That repeat is deliberate and it is the realistic case: a refrain's words turn up early,
    and this song's own second chorus LCS-matched its closing line to the outro's identical
    words 24 seconds later. It is what makes the difference between timing a line inside its
    own block's span and timing it against the whole transcript **observable** rather than a
    property nobody's fixture could tell apart.
    """
    words = [
        (heard_word(block, index), float(block * 10 + index), block * 10 + index + 0.9)
        for block in range(6)
        for index in range(8)
    ]
    for offset, index in enumerate((5, 6, 7)):
        words[index] = (heard_word(2, offset), words[index][1], words[index][2])
    return words


def measured_project(
    *, lyrics: str = "", words: list[tuple[str, float, float]] | None = None
) -> Project:
    """A project whose song carries word-level times, and no shots."""
    return Project(
        id="project_phrases",
        name="Phrases",
        song=Song(
            title="Phrases",
            source="imported",
            path="media/song.flac",
            duration=PHRASE_SONG_DURATION,
            lyrics=lyrics,
            lyric_words=phrase_words() if words is None else words,
        ),
    )


def tiled(project: Project, windows=PHRASE_WINDOWS) -> Project:
    """The same project with one draft shot per window — the timeline door's input."""
    project = project.model_copy(deep=True)
    project.shots = [Shot(start=start, duration=length) for start, length in windows]
    return project


def layout_of(project: Project, windows=PHRASE_WINDOWS, *, sections=()):
    """A `ShotLayout` carrying nothing but geometry — the line-up door's input."""
    return app_module.ShotLayout(
        project=project,
        duration=project.song.duration if project.song else 0.0,
        required=len(windows),
        proposals=(),
        windows=tuple(windows),
        sections=tuple(sections),
        sections_origin="",
        message="",
    )


def skip_kind(reason: str) -> str:
    """Which of `timeline`'s skip sentences this is, without its shot names in it.

    The two doors name their windows differently — `SHOT 03` on a timeline, `WINDOW 03` on a
    layout that has no shots yet — so the *reasons* cannot be compared verbatim. What has to
    agree is which reason fired for which cut, which is what this reads.
    """
    for phrase, kind in (
        ("already lands clear", "already-silent"),
        ("sits on the boundary of", "section-boundary"),
        ("no voiceless gap within", "out-of-tolerance"),
        ("which leaves", "out-of-band"),
        ("is locked", "locked"),
        ("approved take", "approved"),
        ("has not finished", "in-flight"),
    ):
        if phrase in reason:
            return kind
    raise AssertionError(f"unrecognised skip reason: {reason!r}")


def boundaries_of(windows) -> list[float]:
    return [round(start, 6) for start, _length in windows]


def test_one_snapping_core_serves_both_doors(tmp_path: Path):
    """**The headline of Phase B.** The timeline route and the line-up step agree, cut for cut.

    Same song, same tiling, same tolerance, same band — one reached through `snap_cut_plan`
    from a project's shots, the other through `line_up_shots` from a layout that has no shots
    at all. They are the same `timeline.snap_window_plan` call underneath, and this is what
    says so with data rather than with a docstring: a divergence here would put a cut in one
    place or another depending on which door the Director came through.
    """
    project = measured_project()
    shot_plan = snap_cut_plan(tiled(project), tolerance=0.75, minimum=4.0, maximum=6.0)
    alignment = app_module.line_up_shots(
        layout_of(project), tolerance=0.75, minimum=4.0, maximum=6.0
    )

    assert shot_plan.moves, "the fixture proposed no move, so this proves nothing"
    assert shot_plan.skips, "the fixture refused no cut, so this proves nothing"
    assert [(move.boundary, move.proposed, move.gap) for move in shot_plan.moves] == [
        (move.boundary, move.proposed, move.gap) for move in alignment.moves
    ]
    assert [(skip.boundary, skip_kind(skip.reason)) for skip in shot_plan.skips] == [
        (skip.boundary, skip_kind(skip.reason)) for skip in alignment.skips
    ]
    # And the geometry the two would write is the same geometry, to the float.
    assert [(start, length) for _id, start, length in shot_plan.windows] == [
        (placement.start, placement.duration) for placement in alignment.placements
    ]
    assert alignment.status == shot_plan.status == "ready"


def test_a_substituted_snap_target_is_observed_through_both_doors(tmp_path, monkeypatch):
    """One core, proved by taking it away: both doors report the substitute's answer.

    `_gap_snap_target` is the single editorial decision in the snapping module — where inside
    a voiceless gap a cut lands. It is patched here to demand a wider clearance than the real
    one does, and **one** patch is enough for both callers, which is only true because there is
    one implementation between them. Two snappers would need two patches, and the second would
    go on answering 9.85 while the first said 9.60.
    """
    project = measured_project()
    unpatched = snap_cut_plan(tiled(project), tolerance=0.75, minimum=4.0, maximum=6.0)

    monkeypatch.setattr(
        timeline_module,
        "_gap_snap_target",
        lambda gap, boundary: min(max(boundary, gap[0] + 0.4), gap[1] - 0.4),
    )
    shot_plan = snap_cut_plan(tiled(project), tolerance=0.75, minimum=4.0, maximum=6.0)
    alignment = app_module.line_up_shots(
        layout_of(project), tolerance=0.75, minimum=4.0, maximum=6.0
    )

    proposed = [move.proposed for move in shot_plan.moves]
    assert proposed == [move.proposed for move in alignment.moves]
    assert proposed and proposed != [move.proposed for move in unpatched.moves], (
        "the substitute changed nothing, so it observed nothing"
    )


#: The same tiling with one authored transition in it: window 4 starts a second before window
#: 3 ends. The overlap's centre is 20.500 s, inside the phrase that runs to 17.900–20.000 s's
#: right-hand edge, so the seam has somewhere to go and the move is a real one.
OVERLAPPING_WINDOWS = tuple(
    (15.0, 6.0) if index == 3 else window
    for index, window in enumerate(PHRASE_WINDOWS)
)


def test_one_snapping_core_serves_both_doors_on_a_plan_with_a_transition_in_it(tmp_path: Path):
    """The Phase B headline, restated for the geometry that used to refuse on both doors.

    An overlap is an authored transition (R-3), and which point of it lands in the silence —
    `timeline.SEAM_POINT`, the overlap's midpoint — is a decision that must be the *same*
    decision on both doors, or a transition would land in one place because the Director came
    through the timeline and another because they came through a lay-out. Same plan, same
    tolerance, same band; the moves agree including the blend length, and the geometry the two
    would write agrees to the float.
    """
    project = measured_project()
    shot_plan = snap_cut_plan(
        tiled(project, OVERLAPPING_WINDOWS), tolerance=0.75, minimum=4.0, maximum=6.0
    )
    alignment = app_module.line_up_shots(
        layout_of(project, OVERLAPPING_WINDOWS), tolerance=0.75, minimum=4.0, maximum=6.0
    )

    blended = [move for move in shot_plan.moves if move.overlap]
    assert blended, "the fixture moved no transition, so this proves nothing"
    assert [(move.boundary, move.proposed, move.overlap) for move in blended] == [(20.5, 19.85, 1.0)]
    assert [
        (move.boundary, move.proposed, move.gap, move.overlap) for move in shot_plan.moves
    ] == [
        (move.boundary, move.proposed, move.gap, move.overlap) for move in alignment.moves
    ]
    assert [(skip.boundary, skip_kind(skip.reason)) for skip in shot_plan.skips] == [
        (skip.boundary, skip_kind(skip.reason)) for skip in alignment.skips
    ]
    assert [(start, length) for _id, start, length in shot_plan.windows] == [
        (placement.start, placement.duration) for placement in alignment.placements
    ]
    # The blend is the length it was, on both doors, and the plan covers what it covered.
    windows = [(start, length) for _id, start, length in shot_plan.windows]
    assert windows[3][0] + windows[3][1] - windows[4][0] == pytest.approx(1.0, abs=1e-9)
    assert windows[0][0] == 0.0
    assert windows[-1][0] + windows[-1][1] == pytest.approx(PHRASE_SONG_DURATION, abs=1e-9)


def test_a_substituted_snap_target_is_observed_through_both_doors_on_a_transition(
    tmp_path, monkeypatch
):
    """One core, proved by taking it away — on the seam shape that needed the new code.

    `_gap_snap_target` is still the single editorial decision about *where inside a gap* a cut
    lands, and the seam arithmetic above it is the single decision about *what the cut of a
    transition is*. One patch moves both doors; two snappers would need two.
    """
    project = measured_project()
    unpatched = snap_cut_plan(
        tiled(project, OVERLAPPING_WINDOWS), tolerance=0.75, minimum=4.0, maximum=6.0
    )

    monkeypatch.setattr(
        timeline_module,
        "_gap_snap_target",
        lambda gap, boundary: min(max(boundary, gap[0] + 0.4), gap[1] - 0.4),
    )
    shot_plan = snap_cut_plan(
        tiled(project, OVERLAPPING_WINDOWS), tolerance=0.75, minimum=4.0, maximum=6.0
    )
    alignment = app_module.line_up_shots(
        layout_of(project, OVERLAPPING_WINDOWS), tolerance=0.75, minimum=4.0, maximum=6.0
    )

    proposed = [move.proposed for move in shot_plan.moves]
    assert proposed == [move.proposed for move in alignment.moves]
    assert proposed and proposed != [move.proposed for move in unpatched.moves], (
        "the substitute changed nothing, so it observed nothing"
    )
    # The transition's own fate changed with the substitute — a wider clearance puts its target
    # out of reach, so the seam that moved unpatched is now skipped — and it changed the same
    # way on both doors. That is the substitution being observed at the seam this test is
    # about, rather than only at the hard cuts around it.
    assert [move.overlap for move in unpatched.moves if move.overlap] == [1.0]
    assert [move.overlap for move in shot_plan.moves if move.overlap] == []
    assert [(skip.boundary, skip_kind(skip.reason)) for skip in shot_plan.skips] == [
        (skip.boundary, skip_kind(skip.reason)) for skip in alignment.skips
    ]
    # And whether it moved or not, the blend is exactly the length it was.
    windows = [(start, length) for _id, start, length in shot_plan.windows]
    assert windows[3][0] + windows[3][1] - windows[4][0] == pytest.approx(1.0, abs=1e-9)


def test_a_protected_shot_is_skipped_by_name_on_both_snapping_doors(tmp_path: Path):
    """Locked, approved and in-flight refuse a cut in the existing wordings.

    The protections live on `window_move_refusal` and reach the core as sentences, so both
    doors that snap a *stored* timeline honour the same three. A layout has no shots, so it
    has nothing to protect — stated in `line_up_windows` rather than checked for.
    """
    project = tiled(measured_project())
    project.shots[2].locked = True
    project.shots[5].approved_output = "media/takes/five.mp4"
    project.shots[8].status = "queued"

    plan = snap_cut_plan(
        project,
        tolerance=0.75,
        minimum=4.0,
        maximum=6.0,
        rendering=frozenset({project.shots[8].id}),
    )
    kinds = {skip_kind(skip.reason) for skip in plan.skips}
    assert {"locked", "approved", "in-flight"} <= kinds
    # And the protected shots' own windows are untouched, to the bit.
    for index in (2, 5, 8):
        assert plan.windows[index] == (
            project.shots[index].id,
            project.shots[index].start,
            project.shots[index].duration,
        )

    # A transition is a seam two shots share however wide it is, so a lock at either of its
    # ends refuses it — in the same sentence, on both doors, and without resizing the blend by
    # moving one of its edges on its own.
    blended = tiled(measured_project(), OVERLAPPING_WINDOWS)
    blended.shots[4].locked = True
    locked = SNAP_LOCKED_REFUSAL.format(shot=app_module.shot_label(blended, blended.shots[4]))
    stored = timeline_module.shot_snap_windows(blended)
    door_one = snap_cut_plan(blended, tolerance=0.75, minimum=4.0, maximum=6.0)
    door_two = app_module.line_up_shots(
        layout_of(blended, OVERLAPPING_WINDOWS),
        tolerance=0.75,
        minimum=4.0,
        maximum=6.0,
        windows=stored,
    )

    assert not [move for move in door_one.moves if move.overlap]
    assert locked in [skip.reason for skip in door_one.skips]
    assert [skip.reason for skip in door_one.skips] == [
        skip.reason for skip in door_two.skips
    ]
    assert door_one.windows[3][1:] == (blended.shots[3].start, blended.shots[3].duration)
    assert door_one.windows[4][1:] == (blended.shots[4].start, blended.shots[4].duration)


def test_the_timeline_door_reads_the_plan_in_song_order_not_manifest_order(tmp_path: Path):
    """A cut is between the shot that plays and the shot that plays next.

    Manifest order and song order differ for any plan a Director has re-ordered by dragging,
    and `ordered_shots` is the one answer to which is which — `expansion_input`'s reason. Read
    in manifest order the "cuts" would be between shots that never touch, and the contiguity
    precondition would reject a plan that tiles the song perfectly.
    """
    project = tiled(measured_project())
    project.shots = list(reversed(project.shots))
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    saved = store.save(project)
    comfy = NullComfy()
    client = TestClient(
        create_app(settings=settings, store=store, comfy=comfy, director=StepDirector())
    )

    plan = snap_cut_plan(saved, tolerance=0.75, minimum=4.0, maximum=6.0)
    assert plan.status == "ready" and plan.moves

    applied = line_up(client, saved.id, tolerance=0.75, confirm_apply=True)
    assert applied["applied"] is True
    fresh = ProjectStore(tmp_path).get(saved.id)
    # Manifest order is untouched; the windows landed on the right shots all the same.
    assert [shot.id for shot in fresh.shots] == [shot.id for shot in saved.shots]
    by_id = {shot.id: (shot.start, shot.duration) for shot in fresh.shots}
    assert [by_id[shot_id] for shot_id, *_ in plan.windows] == [
        (start, duration) for _id, start, duration in plan.windows
    ]
    assert comfy.prompts == []


def test_tolerance_zero_examines_nothing_and_moves_nothing(tmp_path: Path):
    """0 is the feature switched off, and off means *nothing was examined*.

    Not a loop that happened to find no candidates: the status says so, and every window comes
    back with the floats it went in with.
    """
    project = measured_project()
    alignment = app_module.line_up_shots(layout_of(project), tolerance=0.0)

    assert alignment.status == "off"
    assert alignment.moved == 0 and alignment.moves == () and alignment.skips == ()
    assert [
        (placement.start, placement.duration) for placement in alignment.placements
    ] == list(PHRASE_WINDOWS)


def test_a_cut_with_no_voiceless_gap_in_reach_stays_where_it_is(tmp_path: Path):
    """The nearest rest is two seconds away, the tolerance is three quarters of one.

    Reported as a skip with the sentence that names the tolerance, never nudged part of the
    way: a cut moved halfway to a rest is a cut still inside a sung phrase.
    """
    alignment = app_module.line_up_shots(
        layout_of(measured_project()), tolerance=0.75, minimum=4.0, maximum=6.0
    )
    stayed = [skip for skip in alignment.skips if skip_kind(skip.reason) == "out-of-tolerance"]
    assert stayed, "no cut was out of reach, so this proves nothing"
    for skip in stayed:
        placement = alignment.placements[
            boundaries_of(
                [(placement.start, placement.duration) for placement in alignment.placements]
            ).index(round(skip.boundary, 6))
        ]
        assert placement.start == skip.boundary, "an out-of-reach cut moved anyway"
        assert "0.75s of it" in skip.reason


def test_the_lined_up_plan_still_tiles_the_song(tmp_path: Path):
    """Contiguity is the invariant line-up may not spend, and a long plan is where it shows.

    Sixty windows over five minutes, so any per-cut rounding would accumulate into a visible
    gap by the end. The tiling starts at exactly 0, ends at exactly the song's length, and no
    two neighbours disagree about their shared boundary by more than assembly's own tolerance.
    """
    duration = 300.0
    words = [
        (heard_word(block, index), float(block * 10 + index), block * 10 + index + 0.9)
        for block in range(30)
        for index in range(8)
    ]
    project = measured_project(words=words)
    project.song.duration = duration
    windows = tuple((float(index * 5), 5.0) for index in range(60))
    alignment = app_module.line_up_shots(
        layout_of(project, windows), tolerance=0.75, minimum=4.0, maximum=6.0
    )

    assert alignment.moved > 10, "too few cuts moved for this to be a rounding test"
    placements = alignment.placements
    assert placements[0].start == 0.0
    assert placements[-1].start + placements[-1].duration == duration
    for previous, current in pairwise(placements):
        assert (
            abs(current.start - (previous.start + previous.duration))
            <= SNAP_CONTIGUITY_TOLERANCE
        )


# --------------------------------------------------------------------------------------------
# The lines a window covers, and who the sheet says sings them.
# --------------------------------------------------------------------------------------------

#: Two `[Tag]` blocks of two sung lines each, against the phrase song's first four phrases.
#: Line 8 is deliberately words the transcript never heard, and line 7 carries a duet mark:
#: the sheet's own `(S1, S2)` grammar, read by `timeline.lyric_line_tags` and written by the
#: per-line dropdown. Sheet line numbers, which are what `LyricLineSpan.index` carries:
#: 0 `[Intro]`, 1 blank, 2 `[Verse]`, 3, 4, 5 blank, 6 `[Chorus]`, 7, 8.
TAGGED_SHEET = (
    "[Intro]\n\n[Verse]\n(S1) aa ab ac\n(S2) ba bb bc\n\n"
    "[Chorus]\n(S1, S2) ca cb cc\nzz zy zx\n"
)
UNTAGGED_SHEET = "[Intro]\n\n[Verse]\naa ab ac\nba bb bc\n\n[Chorus]\nca cb cc\nzz zy zx\n"


def lines_of(placement) -> list[tuple[int, tuple[int, ...]]]:
    return [(line.index, line.slots) for line in placement.lines]


def test_a_window_is_handed_the_lines_it_covers_and_their_singers(tmp_path: Path):
    """The seam multi-character pass 2 reads: which lines, and which singer slots.

    The first line is sung from 0.0 s and the second from 10.0 s, so a window spanning both
    reports both and names both singers, while a window over the first alone reports one. The
    facts are produced here and consumed nowhere: no citation is chosen from a slot, which is
    fill-in's job and is deliberately not built in this phase.
    """
    project = measured_project(lyrics=TAGGED_SHEET)
    alignment = app_module.line_up_shots(
        layout_of(project, ((0.0, 15.0), (15.0, 45.0))), tolerance=0.0
    )

    both, rest = alignment.placements
    assert lines_of(both) == [(3, (1,)), (4, (2,))]
    assert both.singers == (1, 2)
    assert [line.text for line in both.lines] == ["aa ab ac", "ba bb bc"]
    # Measured, not shared out: each line's span is its own first and last heard word.
    assert (both.lines[0].start, both.lines[0].end) == (0.0, 2.9)
    assert (both.lines[1].start, both.lines[1].end) == (10.0, 12.9)
    # The chorus's own block, and only its own lines. A line whose words were never heard —
    # line 8 — is omitted rather than given the gap between its neighbours.
    assert lines_of(rest) == [(7, (1, 2))]
    assert rest.singers == (1, 2)
    assert (rest.lines[0].start, rest.lines[0].end) == (20.0, 22.9)

    narrow = app_module.line_up_shots(
        layout_of(project, ((0.0, 5.0), (5.0, 55.0))), tolerance=0.0
    )
    assert lines_of(narrow.placements[0]) == [(3, (1,))]
    assert narrow.placements[0].singers == (1,)
    # One window over the whole song names each slot once, in order — `singers` is a set of
    # what the lines say, not a running list of every mark it passed.
    whole = app_module.line_up_shots(
        layout_of(project, ((0.0, PHRASE_SONG_DURATION),)), tolerance=0.0
    )
    assert lines_of(whole.placements[0]) == [(3, (1,)), (4, (2,)), (7, (1, 2))]
    assert whole.placements[0].singers == (1, 2)


def test_an_untagged_sheet_reports_its_lines_without_inventing_a_singer(tmp_path: Path):
    """`()` is untagged, and untagged is never "the first singer".

    Every sheet written before per-line marks existed is in this state, and the lines are still
    worth having: what a shot's window covers is a fact about the song, and who sings it is a
    fact about the sheet. Missing the second must not cost the first.
    """
    project = measured_project(lyrics=UNTAGGED_SHEET)
    alignment = app_module.line_up_shots(
        layout_of(project, ((0.0, 15.0), (15.0, 45.0))), tolerance=0.0
    )
    covered = alignment.placements[0]
    assert lines_of(covered) == [(3, ()), (4, ())]
    assert covered.singers == ()
    assert lines_of(alignment.placements[1]) == [(7, ())]


def test_line_up_keeps_the_layouts_own_window_ceiling(tmp_path: Path):
    """A step that nudges a cut may not undo the length decision of the step before it.

    Lay-out caps its windows at `POPULATE_MAX_WINDOW_SECONDS` on a measured render-cost
    decision — per-frame sampling cost on `turbo-references2v` is flat out to 175 frames
    (6.79 s) and then climbs +48%, +184% and +264% over the next three steps, so the ceiling is
    the top of the flat region. See the constant's own docstring for the curve and for the three
    earlier justifications it replaced. A move that would push a window past that ceiling is
    refused *there*, by the band, and named as such. A caller lining up stored windows passes
    the wider 4–15 s band instead, which is `snap_window_plan`'s own argument.

    **6.0 is passed here as a literal and is deliberately not the constant.** What is under test
    is that `line_up_shots` honours *whatever* band it is given, so the number has to be the
    test's rather than the application's — and the geometry below is tuned to it. The constant
    moved to 6.8 on 2026-08-23 and this test did not, which is the point: it would have to keep
    passing at any ceiling.
    """
    project = measured_project()
    # The cut at 10.6 s sits inside the second sung phrase; the rest at 7.9–10.0 is in reach
    # and would take it to 9.85 s — leaving the first window 9.85 s long, well over the 6 s
    # ceiling passed below and comfortably inside H3's own 15 s one.
    windows = ((0.0, 10.6), (10.6, 7.4), (18.0, 42.0))
    tight = app_module.line_up_shots(
        layout_of(project, windows), tolerance=0.75, minimum=4.0, maximum=6.0
    )
    assert tight.moved == 0
    assert skip_kind(tight.skips[0].reason) == "out-of-band"
    assert "over the 6s maximum" in tight.skips[0].reason

    wide = app_module.line_up_shots(
        layout_of(project, windows), tolerance=0.75, minimum=4.0, maximum=15.0
    )
    assert wide.moved == 1, "the wider band refused the same move"


def test_a_block_header_is_never_reported_as_a_line_somebody_sang(tmp_path: Path):
    """`[Verse]` is markup. Nobody sings it, and no window may be told they did.

    A block owns the lines *after* its header, and this pins the "after". The sheet here names
    its block `[Aa]` — a tag whose own word the transcript really does contain, at 0.0 s —
    because a tag that could never match would make the rule untestable rather than true.
    """
    project = measured_project(lyrics="[Aa]\naa ab ac\n")
    alignment = app_module.line_up_shots(
        layout_of(project, ((0.0, 15.0),)), tolerance=0.0
    )
    assert lines_of(alignment.placements[0]) == [(1, ())]
    assert [line.text for line in alignment.placements[0].lines] == ["aa ab ac"]


def test_the_line_facts_survive_the_round_trip_through_a_report(tmp_path: Path):
    """The seam has to reach the next step, and the next step reads the report, not memory.

    A line-up report is what fill-in is handed, so a line fact that travelled onto the wire
    and did not come back would be a seam that exists only inside one function call. Pass 2
    will read the cast from exactly this object.

    `singers` is **not** read back off the wire: it is derived from `lines` by the property,
    so a hand-edited body cannot claim a singer no line on the row names.
    """
    project = measured_project(lyrics=TAGGED_SHEET)
    alignment = app_module.line_up_shots(
        layout_of(project, ((0.0, 15.0), (15.0, 45.0))), tolerance=0.0
    )
    report = app_module.alignment_report(alignment, app_module.layout_report(alignment.layout))
    restored = app_module.alignment_from_report(project, report)

    assert [lines_of(placement) for placement in restored.placements] == [
        lines_of(placement) for placement in alignment.placements
    ]
    assert [placement.singers for placement in restored.placements] == [
        placement.singers for placement in alignment.placements
    ]
    assert [
        (line.text, line.start, line.end)
        for placement in restored.placements
        for line in placement.lines
    ] == [
        (line.text, line.start, line.end)
        for placement in alignment.placements
        for line in placement.lines
    ]
    # A body claiming a singer none of its lines name is answered by the lines, not by itself.
    report.windows[0].singers = [4]
    assert app_module.alignment_from_report(project, report).placements[0].singers == (1, 2)


def test_an_unmeasured_song_gets_no_snapping_and_no_line_facts(tmp_path: Path):
    """The explicitly empty branch: nothing heard means nothing placed and nothing claimed.

    Not a guess, not a crash, and not "the whole song is a gap" — a track nobody transcribed is
    **unmeasured, not silent**, and every consequence of that is stated rather than inferred.
    """
    project = measured_project(lyrics=TAGGED_SHEET, words=[])
    alignment = app_module.line_up_shots(layout_of(project), tolerance=0.75)

    assert alignment.status == "unmeasured"
    assert alignment.moved == 0 and alignment.moves == ()
    assert alignment.measured is False
    assert [
        (placement.start, placement.duration) for placement in alignment.placements
    ] == list(PHRASE_WINDOWS)
    assert all(placement.lines == () for placement in alignment.placements)
    assert all(placement.singers == () for placement in alignment.placements)
    assert all(placement.vocal_seconds is None for placement in alignment.placements)


# --------------------------------------------------------------------------------------------
# Line-up on the wire: the chain's single confirmation, and the standalone report-then-confirm.
# --------------------------------------------------------------------------------------------


def test_a_chained_populate_moves_its_cuts_onto_phrase_boundaries(tmp_path: Path):
    """What Phase B changes about a populate, measured against the plan it used to write.

    Two runs of the same model reply: one with snapping off, which is the pre-Phase-B plan to
    the byte, and one at the default tolerance. Every boundary that differs is checked to sit
    at least `SNAP_CLEARANCE_SECONDS` inside a stretch the track is *measured* to leave
    voiceless — the property the feature exists for, rather than merely "some numbers moved".

    **Run on the word-timed arm since 2026-08-23, and the reason is a measurement.** When
    `POPULATE_MAX_WINDOW_SECONDS` moved 6.0 → 6.8 this test's `words=False` fixture stopped
    offering any movable cut at all: on the merged *display* view of the voice there are five
    gaps in 154.6 s, and of the 26 cuts a 6.8 s ceiling lays, exactly two come within the 0.75 s
    tolerance of one — 89.635 s, whose move to 88.990 s would leave the next window **7.410 s**,
    and 119.862 s, whose move to 120.250 s would leave the previous one **3.850 s**. Both are
    refused by the band, correctly, so `moved` was 0 and the test could no longer see what it is
    about. (At 6.0 it saw exactly one move, 87.466 → 87.870, so the arm was already threadbare.)
    The word-timed arm is the path production takes — `vocal_gaps` reads `Song.lyric_words` and
    the Director's song has 262 of them — and there 8 of 26 cuts move at 6.8, against 12 of 28 at
    6.0. **Nothing in the claim below was relaxed**; every moved boundary is still required to
    sit `SNAP_CLEARANCE_SECONDS` inside a measured voiceless stretch.
    """
    still_client, _s, _c, _d, still_id = make_client(tmp_path / "still", words=True)
    populate(still_client, still_id, snap_tolerance=0)
    snapped_client, _store, comfy, _director, snapped_id = make_client(
        tmp_path / "snapped", words=True
    )
    body = populate(snapped_client, snapped_id)

    still = ProjectStore(tmp_path / "still").get(still_id)
    snapped = ProjectStore(tmp_path / "snapped").get(snapped_id)
    assert len(still.shots) == len(snapped.shots)
    assert body["moved"] > 0, "the default tolerance moved nothing"
    assert body["moved"] + body["skipped"] == len(snapped.shots) - 1

    gaps = vocal_gaps(snapped.song, start=0.0, end=snapped.song.duration)
    changed = [
        after.start
        for before, after in zip(still.shots[1:], snapped.shots[1:])
        if before.start != after.start
    ]
    assert len(changed) == body["moved"]
    for boundary in changed:
        assert any(
            low + SNAP_CLEARANCE_SECONDS - 1e-9 <= boundary <= high - SNAP_CLEARANCE_SECONDS + 1e-9
            for low, high in gaps
        ), f"a cut moved to {boundary}, which is not clear of every sung phrase"
    # And the plan still tiles the song, which is what fill-in and assembly both depend on.
    assert snapped.shots[0].start == 0.0
    for previous, current in pairwise(snapped.shots):
        assert abs(current.start - previous.end) <= SNAP_CONTIGUITY_TOLERANCE
    # Lay-out's window ceiling survives the step after it. Line-up nudges cuts; it is not the
    # place a window grows past the cap lay-out laid it under, and the measured render cost past
    # that cap is the whole reason `POPULATE_MAX_WINDOW_SECONDS` exists. Asserted against the
    # constant rather than a literal, so it followed the 6.0 -> 6.8 ruling without an edit —
    # and it is not vacuous at 6.8: five of the 27 windows here sit within 5 ms of the cap.
    assert max(shot.duration for shot in snapped.shots) <= POPULATE_MAX_WINDOW_SECONDS + 1e-9
    assert comfy.prompts == []


def test_the_chain_asks_for_no_second_confirmation_to_move_a_cut(tmp_path: Path):
    """One confirmation for the whole first pass — `confirm_replace`, asked by lay-out.

    Line-up moves windows inside a chained populate without a confirm of its own, and that is
    the ruling rather than an omission: the consent above it is consent to replace this
    timeline's windows, and the windows being moved were created by the very call it consented
    to. There is no shot on the timeline for a protection to be about — `lay_out_protections`
    has already refused if there were.

    **`words=True` since 2026-08-23**, for the reason given on
    `test_a_chained_populate_moves_its_cuts_onto_phrase_boundaries`: at a 6.8 s ceiling the
    merged-span arm offers no cut the band will let move, so `moved > 0` — the precondition this
    test needs before it can say anything about confirmations — is only true on the word-timed
    arm, which is the one production takes.
    """
    client, _store, comfy, _director, project_id = make_client(tmp_path, words=True)
    body = populate(client, project_id)
    assert body["moved"] > 0
    assert ProjectStore(tmp_path).get(project_id).shots, "the chain wrote nothing"
    assert comfy.prompts == []


def test_a_standalone_line_up_reports_before_it_writes_and_the_confirm_persists(
    tmp_path: Path,
):
    """The pass a Director runs over a plan they have been editing. `snap-cuts`' shape.

    No lay-out report, no model, and no write until `confirm_apply` — checked through a fresh
    `ProjectStore` on every claim, because a report that saved an unchanged project would
    leave the same shots behind and would still have written.
    """
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    project = store.save(tiled(measured_project(lyrics=TAGGED_SHEET)))
    comfy = NullComfy()
    client = TestClient(
        create_app(settings=settings, store=store, comfy=comfy, director=StepDirector())
    )
    untouched = ProjectStore(tmp_path).get(project.id).updated_at

    report = line_up(client, project.id, tolerance=0.75)
    assert report["applied"] is False and report["project"] is None
    assert report["layout"] is None, "a project-sourced report claimed a lay-out"
    assert report["status"] == "ready" and report["tolerance"] == 0.75
    assert report["moved"] > 0 and report["skipped"] > 0
    assert len(report["moves"]) == report["moved"]
    assert len(report["skips"]) == report["skipped"]
    assert all(move["before"].startswith("SHOT") for move in report["moves"])
    # The line facts reach the wire, singer slots and all — this is the body the fill-in step
    # will read the cast from, and it is the whole of what Phase B produces for it.
    lined = [row for row in report["windows"] if row["lines"]]
    assert lined, "no window on the wire carried a lyric line"
    assert [row["singers"] for row in lined] == [[1], [2], [1, 2]]
    assert [line["index"] for row in lined for line in row["lines"]] == [3, 4, 7]
    assert ProjectStore(tmp_path).get(project.id).updated_at == untouched

    applied = line_up(client, project.id, tolerance=0.75, confirm_apply=True)
    assert applied["applied"] is True
    fresh = ProjectStore(tmp_path).get(project.id)
    assert [shot.start for shot in fresh.shots] == [
        row["start"] for row in applied["windows"]
    ]
    assert [shot.start for shot in fresh.shots] != [start for start, _ in PHRASE_WINDOWS]
    assert fresh.shots[0].start == 0.0
    for previous, current in pairwise(fresh.shots):
        assert abs(current.start - previous.end) <= SNAP_CONTIGUITY_TOLERANCE
    assert comfy.prompts == []


def test_a_standalone_line_up_refuses_the_cuts_of_a_protected_shot(tmp_path: Path):
    """Locked, approved and in-flight, in the existing wordings, on the route.

    The protections are `window_move_refusal`'s and reach this route through the one builder
    `snap-cuts` also calls, so a cut protected on one of them is protected on the other.
    """
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    project = tiled(measured_project())
    project.shots[2].locked = True
    project.shots[5].approved_output = "media/takes/five.mp4"
    project.jobs = [
        RenderJob(
            kind="h3",
            prompt_id="prompt-in-flight",
            status="queued",
            target_id=project.shots[8].id,
        )
    ]
    saved = store.save(project)
    comfy = NullComfy()
    client = TestClient(
        create_app(settings=settings, store=store, comfy=comfy, director=StepDirector())
    )

    report = line_up(client, saved.id, tolerance=0.75, confirm_apply=True)
    kinds = {skip_kind(skip["reason"]) for skip in report["skips"]}
    assert {"locked", "approved", "in-flight"} <= kinds
    fresh = ProjectStore(tmp_path).get(saved.id)
    for index in (2, 5, 8):
        assert (fresh.shots[index].start, fresh.shots[index].duration) == PHRASE_WINDOWS[index]
    assert comfy.prompts == []


def test_a_standalone_line_up_refuses_a_project_with_no_song(tmp_path: Path):
    """There is no clock to line a window up against. `snap-cuts`' refusal, in its words."""
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    saved = store.save(Project(id="project_nosong", name="No song"))
    comfy = NullComfy()
    client = TestClient(
        create_app(settings=settings, store=store, comfy=comfy, director=StepDirector())
    )
    response = client.post(f"/api/projects/{saved.id}/timeline/line-up", json={})
    assert response.status_code == 422
    assert "song" in response.json()["detail"]
    assert comfy.prompts == []


def test_a_standalone_line_up_refuses_an_unmeasured_song_rather_than_guessing(tmp_path: Path):
    """`snap-cuts`' honest-empty refusal, in its own words: no cut was examined."""
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    saved = store.save(tiled(measured_project(words=[])))
    comfy = NullComfy()
    client = TestClient(
        create_app(settings=settings, store=store, comfy=comfy, director=StepDirector())
    )
    response = client.post(f"/api/projects/{saved.id}/timeline/line-up", json={})
    assert response.status_code == 422
    assert "Nothing has been heard on this track yet" in response.json()["detail"]
    assert [
        (shot.start, shot.duration) for shot in ProjectStore(tmp_path).get(saved.id).shots
    ] == list(PHRASE_WINDOWS)
    assert comfy.prompts == []


def standalone_line_up_client(tmp_path: Path, project: Project):
    """A client over one saved project, for the `line-up` route's project-sourced door."""
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    saved = store.save(project)
    comfy = NullComfy()
    client = TestClient(
        create_app(settings=settings, store=store, comfy=comfy, director=StepDirector())
    )
    return client, saved, comfy


def test_a_standalone_line_up_refuses_a_hole_and_lines_up_an_overlap(tmp_path: Path):
    """The restated precondition, both halves, on the door that found the problem.

    **Found by pointing this route at the Director's live plan** (2026-08-21), which holds
    sixteen seams outside assembly's tolerance. Fifteen of them are overlaps — authored
    transitions under R-3 — and those are now snapped as units rather than refused. The
    sixteenth is a real 22 ms hole, and a hole still refuses: `assembly.tiling_refusals`
    refuses the export for it too, and there is no seam in a stretch with no picture in it.

    Same fixture, same route, one shot moved a quarter second one way or the other. That is
    what makes this a test about the *sign* of the disagreement rather than about two
    unrelated plans.
    """
    holed = tiled(measured_project())
    # A quarter-second hole: shot 4 starts after shot 3 ends, and nothing covers the gap.
    holed.shots[3].start += 0.25
    holed.shots[3].duration -= 0.25
    client, saved, comfy = standalone_line_up_client(tmp_path, holed)

    response = client.post(
        f"/api/projects/{saved.id}/timeline/line-up", json={"confirm_apply": True}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "has a hole in it" in detail
    assert "SHOT 03" in detail and "SHOT 04" in detail
    assert [
        (shot.start, shot.duration) for shot in ProjectStore(tmp_path).get(saved.id).shots
    ] == [(shot.start, shot.duration) for shot in saved.shots]
    assert comfy.prompts == []

    # An overlap in the same plan is lined up rather than refused, and the blend is exactly as
    # long afterwards as the Director made it — asserted on the manifest, through the route.
    overlapped = tiled(measured_project(), OVERLAPPING_WINDOWS)
    client, saved, comfy = standalone_line_up_client(tmp_path / "overlap", overlapped)

    applied = client.post(
        f"/api/projects/{saved.id}/timeline/line-up",
        json={"confirm_apply": True, "tolerance": 0.75},
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["applied"] is True and body["moved"] > 0
    # The wire says which of its moves is a transition and how wide the blend is, so a reader
    # knows the instant it names is a centre rather than a clip edge.
    assert [row["overlap"] for row in body["moves"] if row["overlap"]] == [1.0]
    assert [row["boundary"] for row in body["moves"] if row["overlap"]] == [20.5]
    shots = sorted(
        ProjectStore(tmp_path / "overlap").get(saved.id).shots, key=lambda shot: shot.start
    )
    assert shots[3].end - shots[4].start == pytest.approx(1.0, abs=1e-9)
    assert shots[0].start == 0.0
    assert shots[-1].end == pytest.approx(PHRASE_SONG_DURATION, abs=1e-9)
    assert comfy.prompts == []


def test_a_lay_out_report_with_a_hole_in_it_is_refused_rather_than_lined_up(tmp_path: Path):
    """The same refusal on the plan-carrying door: no picture in the hole, no seam to place.

    `lay_out_shots` tiles contiguously and the digest stops a report being edited on its way
    back, so this is a body no ordinary path produces — which is exactly why it is answered
    rather than assumed away, `FILL_IN_WINDOWS_MOVED`'s rule.
    """
    client, store, comfy, _director, project_id = make_client(tmp_path)
    saved = store.get(project_id)
    plan = {
        "duration": saved.song.duration,
        "required": 3,
        "proposed": 3,
        "created": 3,
        # A one-second hole between the second and third window.
        "windows": [
            {"index": 0, "start": 0.0, "duration": 50.0},
            {"index": 1, "start": 50.0, "duration": 50.0},
            {"index": 2, "start": 101.0, "duration": 53.644},
        ],
        "proposals": [],
        "sections": [],
        "sections_origin": "director",
        "updated_at": saved.updated_at.isoformat(),
    }
    plan["plan_id"] = app_module.plan_fingerprint(
        saved, app_module.LayOutResponse.model_validate(plan)
    )

    response = client.post(
        f"/api/projects/{project_id}/timeline/line-up", json={"plan": plan}
    )
    assert response.status_code == 422
    assert "has a hole in it" in response.json()["detail"]
    assert ProjectStore(tmp_path).get(project_id).shots == []
    assert comfy.prompts == []


def test_a_line_up_confirm_refuses_a_layout_the_timeline_does_not_hold(tmp_path: Path):
    """Its confirm moves the windows a lay-out created; it refuses to move anything else."""
    client, _store, comfy, _director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    response = client.post(
        f"/api/projects/{project_id}/timeline/line-up",
        json={"plan": layout, "confirm_apply": True},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == LINE_UP_WINDOWS_CHANGED.format(
        expected=len(layout["windows"]), found=0
    )
    assert ProjectStore(tmp_path).get(project_id).shots == []
    assert comfy.prompts == []


# --------------------------------------------------------------------------------------------
# Protections, at the step that would violate them and nowhere else.
# --------------------------------------------------------------------------------------------


def test_lay_out_refuses_a_protected_plan_and_fill_in_does_not(tmp_path: Path):
    """The placement rule: lay-out replaces windows, fill-in writes inside them.

    A locked shot and an approved take refuse a lay-out by name — a protection that vanishes
    with the timeline it protected was never a protection. They refuse **nothing** at the
    fill-in step, and that is the property the re-runnable content pass is built on.
    """
    client, store, comfy, director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    client.post(
        f"/api/projects/{project_id}/timeline/lay-out",
        json={"confirm_replace": True, "plan": layout},
    )
    layout = client.post(
        f"/api/projects/{project_id}/timeline/lay-out",
        json={"confirm_replace": True, "plan": layout},
    )
    # Re-read the applied plan, then lock one shot and approve another.
    project = store.get(project_id)
    project.shots[0].locked = True
    project.shots[1].approved_output = "media/takes/one.mp4"
    project.shots[1].status = "approved"
    saved = store.save(project)

    refused = client.post(
        f"/api/projects/{project_id}/timeline/lay-out", json={"two_stage": False}
    )
    assert refused.status_code == 422
    assert "carry protections" in refused.json()["detail"]
    assert len(director.requests) == 1, "the model was asked despite the refusal"

    chained = client.post(
        f"/api/projects/{project_id}/timeline/populate",
        json={"confirm_replace": True},
    )
    assert chained.status_code == 422
    assert "carry protections" in chained.json()["detail"]

    # Fill in, over the very same protected plan, is not refused. The windows are built from
    # the live timeline, so the plan it is handed is one lined up from it.
    layout_plan = {
        "duration": saved.song.duration,
        "required": len(saved.shots),
        "proposed": len(saved.shots),
        "created": len(saved.shots),
        "windows": [
            {"index": index, "start": shot.start, "duration": shot.duration}
            for index, shot in enumerate(saved.shots)
        ],
        "proposals": [
            {
                "index": index,
                "start": shot.start,
                "duration": shot.duration,
                "prompt": "A new intent for this window.",
                "performance": True,
                "assets": ["HarderFaster"],
            }
            for index, shot in enumerate(saved.shots)
        ],
        "sections": [],
        "sections_origin": "director",
        "updated_at": saved.updated_at.isoformat(),
    }
    layout_plan["plan_id"] = app_module.plan_fingerprint(
        saved, app_module.LayOutResponse.model_validate(layout_plan)
    )
    # Tolerance 0: this test is about *protections*, and it holds the windows still so that
    # what fill-in writes can be compared against the plan it was handed. That a plan-carrying
    # line-up over a protected timeline moves nothing it may not is
    # `test_a_protected_shot_is_skipped_by_name_on_both_snapping_doors`' claim.
    aligned = line_up(client, project_id, layout_plan, tolerance=0)
    written = fill_in(client, project_id, aligned, confirm_apply=True)
    assert written["applied"] is True
    fresh = ProjectStore(tmp_path).get(project_id)
    assert fresh.shots[0].locked is True, "fill-in cleared a lock"
    assert fresh.shots[1].approved_output == "media/takes/one.mp4", "fill-in dropped a take"
    assert all(shot.prompt == "A new intent for this window." for shot in fresh.shots)
    assert comfy.prompts == []


def test_lay_out_refuses_while_a_render_is_in_flight(tmp_path: Path):
    """A render executing now was submitted for the shots this step would replace.

    `lay_out_protections`' second refusal, and it is asked before the model is spent — on the
    step's own route and on the chain alike.
    """
    client, store, comfy, director, project_id = make_client(tmp_path)
    project = store.get(project_id)
    project.shots = [Shot(start=0, duration=5, prompt="A held frame.")]
    project.jobs = [
        RenderJob(kind="h3", prompt_id="prompt-in-flight", status="queued", target_id="x")
    ]
    store.save(project)

    for route, body in (
        ("lay-out", {}),
        ("populate", {"confirm_replace": True}),
    ):
        response = client.post(
            f"/api/projects/{project_id}/timeline/{route}", json=body
        )
        assert response.status_code == 409, route
        assert "Renders are in flight" in response.json()["detail"], route
    assert director.requests == [], "the model was asked despite the refusal"
    assert len(ProjectStore(tmp_path).get(project_id).shots) == 1
    assert comfy.prompts == []


def test_the_report_says_where_the_section_layer_came_from(tmp_path: Path):
    """`sections_origin` is how a Director tells a structure they marked from one they got.

    It is also what decides whether the confirm rewrites the section layer at all, so a report
    that misnamed the origin would silently re-mint the ids of boxes nobody touched.
    """
    client, _store, _comfy, _director, project_id = make_client(tmp_path / "director")
    assert lay_out(client, project_id)["sections_origin"] == "director"

    client, _store, _comfy, _director, project_id = make_client(
        tmp_path / "structure", sections=False
    )
    staged = lay_out(client, project_id, two_stage=True)
    assert staged["sections_origin"] == "structure"
    # The structure pass's own answer, not the shots call's second one.
    assert staged["sections"][0]["prompt"].startswith("[c0]")

    client, _store, _comfy, _director, project_id = make_client(
        tmp_path / "shots", sections=False
    )
    volunteered = lay_out(client, project_id)
    assert volunteered["sections_origin"] == "shots"
    assert volunteered["sections"][0]["prompt"].startswith("[c0]")


def test_the_chain_still_refuses_without_confirm_replace(tmp_path: Path):
    """Populate's own consent, in populate's own words, before the model is spent."""
    client, _store, comfy, director, project_id = make_client(tmp_path)
    response = client.post(
        f"/api/projects/{project_id}/timeline/populate", json={"confirm_replace": False}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == POPULATE_CONFIRM_REFUSAL
    assert director.requests == [], "the model was asked before the consent was checked"
    assert ProjectStore(tmp_path).get(project_id).shots == []
    assert comfy.prompts == []


def test_a_song_less_project_refuses_at_the_step_that_needs_the_song(tmp_path: Path):
    """The refusal is `lay_out_protections`', which both the route and the chain call."""
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    project = store.save(Project(id="project_nosong", name="No song"))
    comfy = NullComfy()
    client = TestClient(
        create_app(settings=settings, store=store, comfy=comfy, director=StepDirector())
    )
    for route, body in (
        ("lay-out", {}),
        ("populate", {"confirm_replace": True}),
    ):
        response = client.post(
            f"/api/projects/{project.id}/timeline/{route}", json=body
        )
        assert response.status_code == 422, route
        assert "needs a song" in response.json()["detail"], route
    assert comfy.prompts == []


# --------------------------------------------------------------------------------------------
# The plan-carrying checks: the layout that lands is the layout that was read.
# --------------------------------------------------------------------------------------------


def test_a_confirm_without_a_report_is_refused_rather_than_re_asked(tmp_path: Path):
    client, _store, _comfy, director, project_id = make_client(tmp_path)
    response = client.post(
        f"/api/projects/{project_id}/timeline/lay-out", json={"confirm_replace": True}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == LAY_OUT_NO_PLAN
    assert director.requests == [], "a confirm asked the model"
    assert ProjectStore(tmp_path).get(project_id).shots == []


def test_a_tampered_report_is_refused_by_its_own_digest(tmp_path: Path):
    client, _store, _comfy, _director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    layout["windows"][3]["duration"] = 12.0

    response = client.post(
        f"/api/projects/{project_id}/timeline/lay-out",
        json={"confirm_replace": True, "plan": layout},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == LAY_OUT_PLAN_MISMATCH
    assert ProjectStore(tmp_path).get(project_id).shots == []

    aligned_response = client.post(
        f"/api/projects/{project_id}/timeline/line-up", json={"plan": layout}
    )
    assert aligned_response.status_code == 422


def test_a_report_read_from_a_stale_revision_is_refused(tmp_path: Path):
    client, store, _comfy, _director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    moved = store.get(project_id)
    moved.name = "Renamed while the report sat on screen"
    store.save(moved)

    for route, body in (
        ("lay-out", {"confirm_replace": True, "plan": layout}),
        ("line-up", {"plan": layout}),
    ):
        response = client.post(
            f"/api/projects/{project_id}/timeline/{route}", json=body
        )
        assert response.status_code == 409, route
        assert response.json()["detail"] == PROJECT_CHANGED_REFUSAL, route
    assert ProjectStore(tmp_path).get(project_id).shots == []


def test_fill_in_needs_a_line_up_report(tmp_path: Path):
    client, _store, _comfy, _director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    aligned = line_up(client, project_id, layout)
    for body in (
        {},
        {"plan": None},
        {"plan": {**aligned, "layout": None}},
        {"plan": {**aligned, "windows": []}},
        {"plan": {**aligned, "plan_id": ""}},
    ):
        response = client.post(
            f"/api/projects/{project_id}/timeline/fill-in", json=body
        )
        assert response.status_code == 422, body
        assert response.json()["detail"] == FILL_IN_NO_PLAN, body

    aligned["windows"][0]["voiceless"] = not aligned["windows"][0]["voiceless"]
    response = client.post(
        f"/api/projects/{project_id}/timeline/fill-in", json={"plan": aligned}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == FILL_IN_PLAN_MISMATCH


def test_a_half_sent_lay_out_report_is_refused_rather_than_read_as_no_plan(tmp_path: Path):
    """An empty or unsigned plan is a broken plan, not an invitation to line up the timeline.

    `plan` absent is the project-sourced entry point (below); `plan` *present* and missing its
    windows or its signature is a body this server did not emit, and reading it as "no plan"
    would silently line up something the caller never asked about.
    """
    client, _store, _comfy, _director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    for body in (
        {"plan": {**layout, "windows": []}},
        {"plan": {**layout, "plan_id": ""}},
    ):
        response = client.post(
            f"/api/projects/{project_id}/timeline/line-up", json=body
        )
        assert response.status_code == 422, body
        assert response.json()["detail"] == LINE_UP_NO_PLAN, body

    # And with no plan at all, on a project whose timeline is still empty: the honest-empty
    # refusal `snap-cuts` gives, in its own words. A cut is a boundary two shots share.
    for body in ({}, {"plan": None}):
        response = client.post(
            f"/api/projects/{project_id}/timeline/line-up", json=body
        )
        assert response.status_code == 422, body
        assert "no cut to snap" in response.json()["detail"], body
    assert ProjectStore(tmp_path).get(project_id).shots == []


def test_fill_in_refuses_when_the_windows_it_was_laid_out_for_moved(tmp_path: Path):
    """Fill in writes inside windows and never moves or makes one.

    A timeline edited between the line-up and the confirm is a timeline whose shot 12 is not
    the shot the report's row 12 was written for — refused rather than written onto whatever
    is at that index now.
    """
    client, store, comfy, _director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    layout = client.post(
        f"/api/projects/{project_id}/timeline/lay-out",
        json={"confirm_replace": True, "plan": layout},
    ).json()
    aligned = line_up(client, project_id, layout)

    dragged = store.get(project_id)
    dragged.shots[4].start += 0.25
    store.save(dragged)
    # The revision moved with the drag, so the revision check speaks first — which is itself
    # the guarantee. Re-mint the plan onto the new revision to reach the window check.
    aligned["updated_at"] = store.get(project_id).updated_at.isoformat()
    aligned["plan_id"] = app_module.plan_fingerprint(
        store.get(project_id), app_module.LineUpResponse.model_validate(aligned)
    )
    response = client.post(
        f"/api/projects/{project_id}/timeline/fill-in",
        json={"plan": aligned, "confirm_apply": True},
    )
    assert response.status_code == 422
    assert "never moves one" in response.json()["detail"]
    assert ProjectStore(tmp_path).get(project_id).shots[4].prompt == ""
    assert comfy.prompts == []


def test_the_window_guarantee_is_checked_rather_than_argued(tmp_path: Path):
    """A fill-in that produced a moved window is refused — on the report, before any write.

    Unreachable while the step reads its geometry from the alignment it was handed, which is
    why it is checked rather than argued; the step is substituted here to reach it.
    """
    client, _store, _comfy, _director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    layout = client.post(
        f"/api/projects/{project_id}/timeline/lay-out",
        json={"confirm_replace": True, "plan": layout},
    ).json()
    # Tolerance 0 so the windows stand still and the substituted step below is the only thing
    # that can move one — which is the whole claim of this test.
    aligned = line_up(client, project_id, layout, tolerance=0)

    real_fill_in = app_module.fill_in_shots

    def moving_fill_in(alignment):
        shots = real_fill_in(alignment)
        shots[0] = Shot(
            start=shots[0].start + 1.0,
            duration=shots[0].duration,
            prompt=shots[0].prompt,
        )
        return shots

    app_module.fill_in_shots = moving_fill_in
    try:
        reported = client.post(
            f"/api/projects/{project_id}/timeline/fill-in", json={"plan": aligned}
        )
        confirmed = client.post(
            f"/api/projects/{project_id}/timeline/fill-in",
            json={"plan": aligned, "confirm_apply": True},
        )
    finally:
        app_module.fill_in_shots = real_fill_in
    # Refused on both, and the report is refused too: a report describing a window the confirm
    # would not write is a report nobody can act on.
    for response in (reported, confirmed):
        assert response.status_code == 500
        assert response.json()["detail"] == FILL_IN_WINDOWS_MOVED
    assert ProjectStore(tmp_path).get(project_id).shots[0].prompt == ""


def test_a_two_stage_chain_spends_its_structure_call_and_no_more(tmp_path: Path):
    """The opt-in structure pass is lay-out's, and the chain still asks the shots call once."""
    client, _store, comfy, director, project_id = make_client(tmp_path, sections=False)
    body = populate(client, project_id, two_stage=True)

    assert len(director.requests) == 2
    assert director.requests[0]["temperature"] is None
    assert [section["label"] for section in body["project"]["sections"]] == [
        label for label, *_ in SECTIONS
    ]
    # The structure pass's own sections landed, not the shots call's second answer.
    assert body["project"]["sections"][0]["prompt"].startswith("[c0]")
    assert comfy.prompts == []


def test_the_steps_add_no_field_to_a_saved_manifest(tmp_path: Path):
    """The intermediates travel on the wire and in memory; nothing new is persisted.

    So there is no defaulted manifest field to enumerate write paths for — the whole-project
    `PUT` cannot be a hole in a guard that does not exist. Pinned rather than asserted in
    prose, because the next phase's narrative role *will* add one.
    """
    client, _store, _comfy, _director, project_id = make_client(tmp_path)
    populate(client, project_id)
    manifest = json.loads(
        (tmp_path / "projects" / project_id / "project.json").read_text(encoding="utf-8")
    )
    assert set(manifest["shots"][0]) <= set(Shot.model_fields) | {"end"}
    assert set(manifest) <= set(Project.model_fields)


# --------------------------------------------------------------------------------------------
# The four defects the first live end-to-end run measured, fixed 2026-08-22.
#
# Each test below fails on the tree that ran that pass. Nothing here calls a model, renders, or
# reads the Director's project; `comfy.prompts` is still asserted empty.
# --------------------------------------------------------------------------------------------

#: Three sections of exactly 20 s over a 60 s song, so a window can be attributed to a section
#: by eye. Marked by the Director, so `sections_origin` is `"director"` and the tiling is the
#: per-section one on every roll.
PAIRING_SECTIONS = [("Alpha", 0.0, 20.0), ("Beta", 20.0, 20.0), ("Gamma", 40.0, 20.0)]

#: Thirteen proposals, deliberately lopsided: six start inside Alpha, **one** inside Beta, six
#: inside Gamma. `populate_required_shots(60)` is 12, so this is a plan lay-out accepts.
#:
#: The tiling then gives Alpha 5 windows, Beta **3** and Gamma 5 — 13 windows against 13
#: proposals — which is the shape that made the defect visible: Beta owns one proposal and
#: several windows, and under the old global-proportion rule its windows drew proposals written
#: for Alpha and for Gamma as well as its own.
#:
#: **Beta held 4 windows until 2026-08-23**, when the Director raised
#: `POPULATE_MAX_WINDOW_SECONDS` from 6.0 to 6.8. Beta is the one section whose count is set by
#: the ceiling rather than by its proposals: it has a single proposal, so `populate_windows`
#: takes the lower clamp `ceil(20 / ceiling)`, which goes 4 → 3, and its windows go 5.000 s to
#: 6.667 s — inside the new band. Alpha and Gamma are pinned by `floor(20 / 4) = 5` against six
#: proposals each and are untouched; their new lower clamp of 3 is below that and never binds.
#: So the whole of the 14 → 13 move is Beta's, and it is the ceiling's arithmetic.
PAIRING_PROPOSAL_STARTS = [
    ("Alpha", 0.0),
    ("Alpha", 3.0),
    ("Alpha", 6.0),
    ("Alpha", 9.0),
    ("Alpha", 12.0),
    ("Alpha", 15.0),
    ("Beta", 20.0),
    ("Gamma", 40.0),
    ("Gamma", 43.0),
    ("Gamma", 46.0),
    ("Gamma", 49.0),
    ("Gamma", 52.0),
    ("Gamma", 55.0),
]


def pairing_client(tmp_path: Path):
    """A 60 s project with three marked sections and the lopsided proposal set above."""
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    project = Project(
        id="project_pairing",
        name="Pairing",
        song=Song(
            title="Pairing",
            source="imported",
            path="media/song.flac",
            duration=60.0,
            # Voice across the middle only, so the outer sections hold real gaps and a cut on a
            # section boundary has somewhere it could otherwise have travelled to.
            vocal_spans=[[12.0, 30.0], [34.0, 48.0]],
        ),
        sections=[
            SongSection(id=f"section_{index}", label=label, start=start, duration=length)
            for index, (label, start, length) in enumerate(PAIRING_SECTIONS)
        ],
    )
    store.save(project)
    script = [
        (
            [
                {
                    "start": start,
                    "duration": 3.0,
                    "prompt": f"{label} proposal {index}",
                    "performance": True,
                    "assets": [],
                }
                for index, (label, start) in enumerate(PAIRING_PROPOSAL_STARTS)
            ],
            [],
        )
    ]
    comfy = NullComfy()
    client = TestClient(
        create_app(
            settings=settings,
            store=store,
            comfy=comfy,
            director=StepDirector(script=script),
        )
    )
    return client, store, comfy, project.id


def section_of(start: float) -> str:
    """Which of `PAIRING_SECTIONS` a window starting here belongs to."""
    return next(
        label for label, at, length in PAIRING_SECTIONS if at <= start < at + length
    )


def test_a_window_only_ever_takes_a_proposal_written_for_its_own_section(tmp_path: Path):
    """**Defect 1.** The Chorus opener that was dropped, and the Verse line that landed in the
    Bridge.

    Lay-out tiles per section and tells the model "every shot sits inside one section and takes
    that section's character"; fill-in used to undo it by mapping proposals to windows by global
    proportion over the whole song. Measured live on 2026-08-21: P7 (the Chorus opener) unused,
    P4 and P12 each used twice on adjacent shots, and four windows carrying prose written for a
    different section — including a Chorus 2 line in the **Bridge**.

    Here Beta owns one proposal and three windows. Every one of its windows must carry Beta's
    line; on the tree that ran the live pass they carried two of Alpha's, Beta's own, and one of
    Gamma's.

    **The two counts below read 14 and 4 until 2026-08-23**, when `POPULATE_MAX_WINDOW_SECONDS`
    moved 6.0 → 6.8 and Beta's window count — the one in this fixture set by the ceiling rather
    than by proposals — went `ceil(20/6.0)=4` to `ceil(20/6.8)=3`. See `PAIRING_PROPOSAL_STARTS`
    for the arithmetic. **The property being tested is unchanged and is still checked over every
    window**: no shot anywhere carries a proposal written for another section.
    """
    client, _store, comfy, project_id = pairing_client(tmp_path)
    body = populate(client, project_id, snap_tolerance=0)
    shots = body["project"]["shots"]

    assert len(shots) == 13
    for shot in shots:
        assert shot["prompt"].startswith(section_of(shot["start"])), shot

    # And the counts, so "never borrows" is not the only thing checked: Beta's single proposal is
    # reused across all of its windows, which is the Director's ruling.
    beta = [shot["prompt"] for shot in shots if section_of(shot["start"]) == "Beta"]
    assert beta == ["Beta proposal 6"] * 3
    assert comfy.prompts == []


def test_no_proposal_is_dropped_inside_a_section_that_has_windows_to_spare(tmp_path: Path):
    """**Defect 1, the other half.** P7 was dropped entirely while P4 and P12 were used twice.

    Within a section holding at least as many windows as proposals the pairing is surjective by
    construction — the rank step is ``p / w <= 1``, so no proposal is skipped. Beta is that reuse
    case; Alpha and Gamma are the mirror, where the step is at least 1 and nothing repeats.
    """
    client, _store, comfy, project_id = pairing_client(tmp_path)
    shots = populate(client, project_id, snap_tolerance=0)["project"]["shots"]

    used = {
        label: [shot["prompt"] for shot in shots if section_of(shot["start"]) == label]
        for label, *_ in PAIRING_SECTIONS
    }
    # Alpha: 5 windows against 6 proposals — every window a different proposal, and the one that
    # goes unused is sampled out of the middle of the section's arc rather than lopped off its
    # tail. Gamma is the same shape, seven proposals along.
    assert used["Alpha"] == [f"Alpha proposal {index}" for index in (0, 1, 3, 4, 5)]
    assert used["Gamma"] == [f"Gamma proposal {index}" for index in (7, 8, 10, 11, 12)]
    # Beta: 4 windows against 1 proposal — reuse, and the section's only line is used.
    assert len(set(used["Beta"])) == 1
    # Nothing anywhere is another section's proposal, which is the ruling stated as a set.
    for label, prompts in used.items():
        assert {prompt.split()[0] for prompt in prompts} == {label}
    assert comfy.prompts == []


def test_a_section_the_model_wrote_nothing_for_is_left_empty_rather_than_borrowing(
    tmp_path: Path,
):
    """The ruling's plain reading: a Chorus window may only ever receive a Chorus proposal.

    So a section with no proposal of its own gets no prose at all — an honestly empty prompt,
    visible in the inspector and in the readiness report, rather than a plausible sentence
    written for the section next door that nobody can see is wrong until the take comes back.
    `singing` is left at the Shot default (`unknown`), never `not_singing`: nothing was declared.
    """
    client, _store, comfy, project_id = pairing_client(tmp_path)
    layout = lay_out(client, project_id)
    # Every proposal Beta owns, removed from the report before the confirm. The digest is
    # re-minted because this is a deliberately hand-edited plan, which is what the digest exists
    # to make deliberate.
    layout["proposals"] = [
        row for row in layout["proposals"] if not (20.0 <= row["start"] < 40.0)
    ]
    layout["plan_id"] = app_module.plan_fingerprint(
        ProjectStore(tmp_path).get(project_id),
        app_module.LayOutResponse.model_validate(layout),
    )
    applied = client.post(
        f"/api/projects/{project_id}/timeline/lay-out",
        json={"confirm_replace": True, "plan": layout},
    ).json()
    aligned = line_up(client, project_id, applied, tolerance=0, confirm_apply=True)
    fill_in(client, project_id, aligned, confirm_apply=True)

    fresh = ProjectStore(tmp_path).get(project_id)
    for shot in fresh.shots:
        if section_of(shot.start) == "Beta":
            assert shot.prompt == "", shot
            assert shot.citations == []
            assert shot.singing == "unknown"
            # Still riding its window of the master: `use_song_audio` is a fact about the
            # window, not a content decision, and the Director's ruling is that every shot
            # gets its piece of the track.
            assert shot.use_song_audio is True
            assert shot.seed != 0
        else:
            assert shot.prompt.startswith(section_of(shot.start)), shot
    assert comfy.prompts == []


def test_a_layout_with_no_sections_at_all_still_maps_by_global_proportion():
    """The compatibility half, and why both chained digests could move without alarm.

    "No sections" in those two arms means *the Director* marked none — `lay_out_shots` adopts
    whatever structure the shots call volunteered, so they tile per section anyway. A layout with
    a genuinely empty section layer is the path `timeline.layout_spans` answers `[]` for, and it
    must go on drawing prompts by proportion over the whole song, exactly as populate always has.
    Asserted on the pure function, because reaching this branch through a route means persuading
    the model to volunteer no structure at all.
    """
    proposals = tuple(
        app_module.ShotProposal(start=index * 10.0, duration=10.0, prompt=f"P{index}")
        for index in range(4)
    )
    layout = app_module.ShotLayout(
        project=Project(id="project_bare", name="Bare"),
        duration=40.0,
        required=4,
        proposals=proposals,
        windows=(),
        sections=(),
        sections_origin="",
        message="",
    )
    midpoints = [2.0, 12.0, 15.0, 22.0, 39.0]
    assert (
        app_module.paired_proposals(layout, midpoints)
        == [
            timeline_module.proposal_for_position(position, 40.0, 4)
            for position in midpoints
        ]
        == [0, 1, 1, 2, 3]
    )


def tiles_exactly(spans, song_duration: float) -> bool:
    """Whether `layout_spans`' answer covers ``[0, song_duration]`` in order with no hole.

    The invariant three separate defects came of not having (2026-08-23), written once so every
    shape below can be held to it rather than to a list of numbers somebody typed.
    """
    cursor = 0.0
    for start, length in spans:
        if abs(start - cursor) > 1e-9 or length <= 0:
            return False
        cursor = start + length
    return abs(cursor - song_duration) <= 1e-9


def test_layout_spans_cuts_the_song_exactly_where_the_section_layer_does():
    """The decomposition both steps now share, asserted on its own rather than through a tiling.

    Lay-out has tiled these spans since sections existed; what is new on 2026-08-22 is that
    fill-in reads the same list, so a disagreement about what a section *is* would put content in
    the wrong window rather than merely tiling oddly. Every branch is here: the unmarked stretch
    before the first section, between two of them, and after the last — each tiled as *its own*
    span only when it is longer than `SPAN_MIN_SECONDS`, because a shorter one is two boxes the
    Director dragged together rather than a piece of song with no section on it.

    **A sub-floor stretch is absorbed rather than dropped (2026-08-23).** The floor decides
    whether a stretch is worth a span, never whether the song is covered — see `SPAN_MIN_SECONDS`
    for the false claim that stood here and the three defects it produced.
    """
    sections = [
        SongSection(label="A", start=2.0, duration=8.0),
        SongSection(label="B", start=13.0, duration=7.0),
        SongSection(label="C", start=20.2, duration=9.8),
    ]

    assert timeline_module.layout_spans([], 60.0) == [], (
        "an unmarked project must answer no spans, not one whole-song span"
    )
    assert timeline_module.layout_spans(sections, 60.0) == [
        (0.0, 2.0),      # the leading gap, tiled: 2 s of song with no section on it
        (2.0, 8.0),
        (10.0, 3.0),     # the gap between A and B
        (13.0, 7.0),
        # B ends 0.2 s before C starts. Under the floor, so not a span of its own — and absorbed
        # forward into C rather than left as a hole, which is what it used to be.
        (20.0, 10.0),
        (30.0, 30.0),    # the trailing gap, tiled
    ]
    assert tiles_exactly(timeline_module.layout_spans(sections, 60.0), 60.0)
    # The same sections against a song that ends 0.4 s after the last one: under the floor, so no
    # trailing span of its own — the last span grows to the song's end instead. **This assertion
    # is the reversal**: it pinned `(20.2, 9.8)` and a 0.4 s tail of uncovered song *as intended*
    # until 2026-08-23, and the intent does not survive. The tiling stays contiguous, so line-up
    # and populate both report success and the export refuses days later with "The song is
    # uncovered from 30.000s to 30.400s". Covering the song is not optional; tiling a 0.4 s span
    # is what the floor was protecting against, and absorbing it does both.
    trailing = timeline_module.layout_spans(sections, 30.4)
    assert [(round(s, 6), round(length, 6)) for s, length in trailing] == [
        (0.0, 2.0), (2.0, 8.0), (10.0, 3.0), (13.0, 7.0), (20.0, 10.4)
    ]
    assert tiles_exactly(trailing, 30.4)


def test_a_section_layer_always_tiles_the_whole_song_whatever_its_shape():
    """**Findings 1, 3, 4 and 7, one root cause: `layout_spans` did not cover the song.**

    Each shape below was reachable and each produced a hole or a duplicate, and every one is
    legal input — `PUT /sections` refuses overlaps only and documents gaps as legal, section
    edges are dragged by hand, and the generic project `PUT` used to validate the field not at
    all. What they have in common is that the tiling stayed *contiguous within itself*, so
    nothing downstream noticed until `snap_window_plan` raised or the export refused.
    """
    def marked(*rows):
        return [
            SongSection(label=label, start=start, duration=length)
            for label, start, length in rows
        ]

    # Finding 1 — a sub-floor gap between two sections. Accepted by `PUT /sections` and
    # reachable by dragging a section edge; it used to emit a 0.3 s hole, which is inside
    # (BOUNDARY_TOLERANCE_SECONDS, SPAN_MIN_SECONDS] and rejected by assembly *and* by the
    # snapper.
    assert timeline_module.layout_spans(
        marked(("Verse", 0.0, 20.0), ("Chorus", 20.3, 19.7)), 40.0
    ) == [(0.0, 20.0), (20.0, 20.0)]

    # Finding 3 — a head stub and a tail stub. The first section starting at 0.4 s left the
    # song uncovered from 0.000s; the last ending 0.4 s early left it uncovered to the end.
    assert timeline_module.layout_spans(
        marked(("Verse", 0.4, 19.6), ("Chorus", 20.0, 20.0)), 40.0
    ) == [(0.0, 20.0), (20.0, 20.0)]
    assert timeline_module.layout_spans(
        marked(("Verse", 0.0, 20.0), ("Chorus", 20.0, 19.6)), 40.0
    ) == [(0.0, 20.0), (20.0, 20.0)]

    # Finding 4 — sections outliving a replaced song. Marked to 180 s, then a 150 s master
    # imported over them: the spans used to run to 180 and populate tiled windows into seconds
    # the song does not have, where `workflows.song_audio_window` refuses at submit.
    assert timeline_module.layout_spans(
        marked(("Verse", 0.0, 100.0), ("Chorus", 100.0, 80.0)), 150.0
    ) == [(0.0, 100.0), (100.0, 50.0)]
    # And a section that begins past the song's end contributes nothing at all — including no
    # unmarked stretch tiled out to a start that does not exist.
    assert timeline_module.layout_spans(marked(("Verse", 200.0, 10.0)), 150.0) == [
        (0.0, 150.0)
    ]

    # Finding 7 — unsorted and overlapping layers, which only `PUT /sections` ever rejected.
    # Unsorted sections used to emit a *duplicate* span, tiling the song twice and raising
    # `SNAP_NESTED`; an overlap is truncated to the section that reached those seconds first.
    assert timeline_module.layout_spans(
        marked(("Chorus", 80.0, 70.0), ("Verse", 0.0, 80.0)), 150.0
    ) == [(0.0, 80.0), (80.0, 70.0)]
    assert timeline_module.layout_spans(
        marked(("A", 0.0, 30.0), ("B", 20.0, 30.0)), 50.0
    ) == [(0.0, 30.0), (30.0, 20.0)]

    for shape, song in (
        (marked(("Verse", 0.0, 20.0), ("Chorus", 20.3, 19.7)), 40.0),
        (marked(("Verse", 0.4, 19.6), ("Chorus", 20.0, 20.0)), 40.0),
        (marked(("Verse", 0.0, 20.0), ("Chorus", 20.0, 19.6)), 40.0),
        (marked(("Verse", 0.0, 100.0), ("Chorus", 100.0, 80.0)), 150.0),
        (marked(("Verse", 200.0, 10.0)), 150.0),
        (marked(("Chorus", 80.0, 70.0), ("Verse", 0.0, 80.0)), 150.0),
        (marked(("A", 0.0, 30.0), ("B", 20.0, 30.0)), 50.0),
        # A whole layer of sub-second marks, each too short for a span of its own.
        (marked(("A", 0.1, 0.2), ("B", 0.4, 0.2), ("C", 0.7, 0.2)), 10.0),
    ):
        assert tiles_exactly(timeline_module.layout_spans(shape, song), song), shape
    # An empty layer still answers `[]` — the emptiness is load-bearing and is not a hole.
    assert timeline_module.layout_spans([], 40.0) == []
    # A song whose length was never recorded clamps nothing rather than clamping to zero.
    assert timeline_module.layout_spans(marked(("Verse", 0.0, 20.0)), 0.0) == [(0.0, 20.0)]


def test_section_edges_names_both_edges_and_lets_a_start_win_a_collision():
    """What the snapper is handed, and it is **both** edges of every section.

    A section that ends where the next begins is one boundary and is named by the section that
    opens there, which is how a Director reads a timeline — `song_section` breaks its own tie the
    same way. A section followed by an unmarked stretch has an *end* that no start covers, and
    that end is a real cut in the tiling, which is why ends are collected at all.
    """
    adjacent = [
        SongSection(label="Intro", start=0.0, duration=11.0),
        SongSection(label="Verse", start=11.0, duration=21.0),
    ]
    assert timeline_module.section_edges(adjacent) == {
        0.0: "Intro", 11.0: "Verse", 32.0: "Verse"
    }
    # With a gap after it, the section's own end is a boundary nothing else names.
    spaced = [
        SongSection(label="Verse", start=0.0, duration=10.0),
        SongSection(label="Chorus", start=20.0, duration=10.0),
    ]
    assert timeline_module.section_edges(spaced) == {
        0.0: "Verse", 10.0: "Verse", 20.0: "Chorus", 30.0: "Chorus"
    }
    assert timeline_module.section_edges([]) == {}


def bare_layout(*, duration: float, proposals, sections=()):
    """A `ShotLayout` carrying only what `paired_proposals` reads."""
    return app_module.ShotLayout(
        project=Project(id="project_pairing_unit", name="Pairing"),
        duration=duration,
        required=len(proposals),
        proposals=tuple(
            app_module.ShotProposal(start=start, duration=3.0, prompt=f"P{index}")
            for index, start in enumerate(proposals)
        ),
        windows=(),
        sections=tuple(sections),
        sections_origin="",
        message="",
    )


def test_pairing_a_layout_that_has_no_proposals_answers_none_rather_than_raising():
    """The project-sourced line-up builds a layout with **no** proposals (`ShotLayout.proposals`
    is `()` there, and the emptiness is what stops such a report being fed to fill-in at all).
    `proposal_for_position` raises on a count of zero, so the guard is what keeps a hand-assembled
    body from turning that honest emptiness into a 500."""
    layout = bare_layout(duration=40.0, proposals=[])
    assert app_module.paired_proposals(layout, [5.0, 15.0]) == [None, None]
    # And with a section layer, which takes the other branch entirely.
    marked = bare_layout(
        duration=40.0,
        proposals=[],
        sections=[SongSection(label="A", start=0.0, duration=40.0)],
    )
    assert app_module.paired_proposals(marked, [5.0, 15.0]) == [None, None]


def test_overlapping_section_boxes_fill_each_window_exactly_once():
    """Section boxes are dragged by hand, and an overlap between two of them settles one way.

    Two boxes over the same seconds would otherwise both offer to fill the windows underneath,
    and the later one would silently win. **Since 2026-08-23 the dispute is settled a step
    earlier**: `timeline.layout_spans` truncates a section that starts behind what is already
    tiled, so Beta's span begins at 30.0 where Alpha's ends rather than at the 20.0 the box
    claims, and the spans handed to the pairing are disjoint whatever the layer does. Alpha's
    seconds are Alpha's, which is the same tie-break `song_section` and `section_edges` make.

    `claimed` and `spent` are then belt and braces on top of that, and they stay: a window may be
    filled once and a proposal read once, which is what "no proposal is used twice" means when
    the span list is not to be trusted.
    """
    layout = bare_layout(
        duration=50.0,
        proposals=[0.0, 5.0, 10.0, 15.0, 25.0, 35.0],
        sections=[
            SongSection(label="Alpha", start=0.0, duration=30.0),
            SongSection(label="Beta", start=20.0, duration=30.0),
        ],
    )
    assert timeline_module.layout_spans(layout.sections, 50.0) == [(0.0, 30.0), (30.0, 20.0)]
    # Windows 0-2 sit in Alpha and take its five proposals sampled 1st/3rd/5th; windows 3-4 sit
    # in Beta, which holds only P5, so both take it — the reuse the ruling permits.
    assert app_module.paired_proposals(layout, [5.0, 15.0, 25.0, 35.0, 45.0]) == [
        0, 2, 4, 5, 5
    ]
    # No proposal crosses the seam: Alpha's five are Alpha's alone, and Beta reads only its own.
    assert 5 not in app_module.paired_proposals(layout, [5.0, 15.0, 25.0])[:3]


def test_no_proposal_is_read_twice_even_if_the_spans_handed_in_overlap(monkeypatch):
    """The `spent` guard, asked the only way it can be asked.

    `claimed` stops a *window* being filled twice and has since overlapping boxes were first
    considered; nothing stopped a *proposal* being read twice, and "no proposal is used twice" is
    one of the two properties `paired_proposals`' docstring rests on. Since 2026-08-23
    `timeline.layout_spans` makes its spans disjoint whatever the section layer does, so the only
    way to put an overlapping decomposition in front of the pairing is to hand it one — which is
    exactly what this test is for. The guard exists for the next thing that produces spans, not
    for a state reachable today, and pinning it is what stops it being deleted as dead.
    """
    layout = bare_layout(
        duration=50.0,
        proposals=[0.0, 5.0, 10.0, 15.0, 25.0, 35.0],
        sections=[SongSection(label="Alpha", start=0.0, duration=50.0)],
    )
    monkeypatch.setattr(
        app_module, "layout_spans", lambda sections, duration: [(0.0, 30.0), (20.0, 30.0)]
    )

    paired = app_module.paired_proposals(layout, [5.0, 15.0, 25.0, 35.0, 45.0])

    # P4 starts at 25.0 s, inside *both* spans. It belongs to the first one that reaches it, and
    # the second must not read it again: without the guard this answers `[0, 2, 4, 4, 5]` and
    # windows 2 and 3 come out with the same prose for two different reasons — one because the
    # section was short of proposals (legitimate reuse, which the ruling permits) and one because
    # two spans read the same list (not).
    assert paired == [0, 2, 4, 5, 5]
    assert paired[3] != paired[2], f"a proposal was read by two spans: {paired}"


def test_a_cut_on_a_section_boundary_is_left_alone_and_says_why(tmp_path: Path):
    """**Defect 3.** Line-up spending the boundary lay-out was built to protect.

    Measured live on 2026-08-21: 2 of 5 moves crossed a section boundary — 11.000 → 10.850 over
    Intro/Verse and 103.200 → 103.050 over Chorus 2/Bridge. The realistic fixture reproduces the
    first exactly: the Intro ends at 11.0 s, the track's first voice starts there, and the
    nearest voiceless moment is 0.15 s earlier.

    The refusal reads in the Director's own terms, beside `snap_window_plan`'s existing
    sentences, and it is that one decision rather than a second mechanism bolted on beside it.
    """
    client, _store, comfy, _director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    aligned = line_up(client, project_id, layout)

    boundaries = {start for _label, start, _length, _prompt in SECTIONS} | {
        start + length for _label, start, length, _prompt in SECTIONS
    }
    for move in aligned["moves"]:
        assert not any(abs(move["boundary"] - at) < 0.5 for at in boundaries), move

    refused = [
        skip for skip in aligned["skips"] if "sits on the boundary of" in skip["reason"]
    ]
    assert refused, "no cut was refused for sitting on a section boundary"
    intro = next(skip for skip in refused if abs(skip["boundary"] - 11.0) < 1e-6)
    assert intro["reason"] == timeline_module.SNAP_SECTION_BOUNDARY.format(
        before=intro["before"], after=intro["after"], boundary=11.0, section="Verse", at=11.0
    )
    assert "no shot straddles a section" in intro["reason"]
    assert comfy.prompts == []


def resection(store: ProjectStore, project_id: str, rows) -> None:
    """Rewrite a project's section layer directly — the state a Director's edge-drag leaves.

    Written past the routes deliberately: these shapes are what `PUT /sections` *accepts* (it
    refuses overlaps and nothing else, and documents gaps as legal) and what a stale manifest
    already holds, so the point is to put the layer on disk and walk the chain over it.
    """
    project = store.get(project_id)
    project.sections = [
        SongSection(
            id=f"section_{index}", label=label, start=start, duration=length, prompt="Look."
        )
        for index, (label, start, length) in enumerate(rows)
    ]
    store.save(project)


@pytest.mark.parametrize(
    "name,rows",
    [
        # A legal sub-floor gap between two sections: 0.3 s, which is inside
        # (`BOUNDARY_TOLERANCE_SECONDS`, `SPAN_MIN_SECONDS`] and a hole to everything that reads
        # a plan. `PUT /sections` accepts it, and dragging a section edge produces it.
        ("interior gap", [("Verse", 0.0, 20.0), ("Chorus", 20.3, 134.344898)]),
        # A head stub: the first section starts 0.4 s in.
        ("head stub", [("Verse", 0.4, 59.6), ("Chorus", 60.0, 94.644898)]),
        # A tail stub: the last section ends 0.4 s early.
        ("tail stub", [("Verse", 0.0, 60.0), ("Chorus", 60.0, 94.244898)]),
        # Sections outliving a replaced song — marked to 180 s over a 154.6 s master.
        ("stale sections", [("Verse", 0.0, 90.0), ("Chorus", 90.0, 90.0)]),
        # Unsorted, which only `PUT /sections` ever rejected; the generic project `PUT` now
        # does too, but a manifest written before it did still holds this.
        ("unsorted", [("Chorus", 80.0, 74.644898), ("Verse", 0.0, 80.0)]),
    ],
)
def test_a_legal_section_layer_never_makes_populate_500(
    tmp_path: Path, name: str, rows
):
    """**Findings 1, 3, 4 and 7 through the chain**, which is where they were 500s.

    Each layer here is legal input and each used to break the whole pass. The gap and the
    unsorted layer raised out of `snap_window_plan` — `SNAP_HOLE` and `SNAP_NESTED` — and
    populate's `line_up_shots` call had no `except TimelineError` where both standalone siblings
    do, so the Director spent a ~110 s model call and got an opaque 500. The two stubs and the
    stale layer were worse than a 500: populate reported *success*, and the export refused days
    later with "The song is uncovered from …", or with shots past the song's end that
    `workflows.song_audio_window` refuses at submit.

    What is asserted is the outcome the Director gets, not an intermediate: 200, a plan that
    covers the song from 0 to its end with no hole assembly would reject, and no window past it.
    """
    client, store, comfy, _director, project_id = make_client(tmp_path)
    resection(store, project_id, rows)

    body = populate(client, project_id, snap_tolerance=0)

    shots = body["project"]["shots"]
    assert shots, name
    cursor = 0.0
    for shot in shots:
        assert abs(shot["start"] - cursor) <= SNAP_CONTIGUITY_TOLERANCE, (name, shot)
        cursor = shot["start"] + shot["duration"]
    assert 0 <= SONG_DURATION - cursor <= SNAP_CONTIGUITY_TOLERANCE, name
    # Nothing past the song: `workflows.song_audio_window` refuses such a window at submit and
    # no proposal reaches those seconds, so those shots could never render and never had prose.
    assert cursor <= SONG_DURATION + 1e-9, name
    assert comfy.prompts == []


def test_a_geometry_the_snapper_refuses_comes_back_in_its_own_words_not_as_a_500(
    tmp_path: Path,
):
    """The other half of finding 1: the chain must **refuse**, never fall through.

    Closing the hole in `layout_spans` is what stops the reachable shapes producing one, but a
    route that answers 500 for anything its core refuses is a route with no answer for the next
    shape nobody has thought of yet. Both standalone siblings translate `TimelineError` to a 422
    carrying the core's own sentence; populate now does too, and the sentence is what the
    Director reads instead of a stack trace they cannot act on.

    Reached by making `layout_spans` answer a holed decomposition, because after the fix no
    section layer can — which is exactly the state this assertion should be left in.
    """
    client, _store, comfy, _director, project_id = make_client(tmp_path)

    def holed(sections, song_duration):
        return [(0.0, 20.0), (20.3, song_duration - 20.3)]

    original = app_module.layout_spans
    app_module.layout_spans = holed
    try:
        response = client.post(
            f"/api/projects/{project_id}/timeline/populate",
            json={"confirm_replace": True, "snap_tolerance": 0.75},
        )
    finally:
        app_module.layout_spans = original

    assert response.status_code == 422, response.text
    assert "This plan has a hole in it" in response.json()["detail"]
    assert comfy.prompts == []


def test_the_pairing_spends_a_short_proposal_list_the_way_it_says_it_does():
    """**Finding 6.** The docstring's two headline clauses were false for reachable ratios.

    Measured rather than argued, because the two claims that were corrected read as if they were
    measured: "the reuse lands in the middle of the section rather than doubling its first or
    last line" and "the surplus is sampled across the section's arc". Both hold for the example
    the docstring gives and for nothing much wider.

    The claims were corrected rather than the arithmetic — `proposal_for_position` is shared with
    the whole-song path and re-spreading it would move every populate digest for a cosmetic
    property — so this pins the behaviour the corrected prose now describes, including the two
    properties that *are* load-bearing.
    """
    def pairing(w: int, p: int) -> list[int]:
        """`paired_proposals`' own arithmetic for one section: w windows, p proposals."""
        return [timeline_module.proposal_for_position(rank + 0.5, w, p) for rank in range(w)]

    # More windows than proposals: the first is doubled and the last tripled, not the middle.
    assert pairing(5, 2) == [0, 0, 1, 1, 1]
    assert pairing(6, 2) == [0, 0, 0, 1, 1, 1]
    # More proposals than windows: with p >= 2w the opener goes, and so does the closer.
    assert pairing(2, 5) == [1, 3]
    assert pairing(2, 4) == [1, 3]
    # The docstring's own example, which is the case both claims were written from.
    assert pairing(3, 5) == [0, 2, 4]

    for windows, proposals_count in ((5, 2), (6, 2), (3, 2), (5, 3), (2, 5), (2, 4), (3, 5)):
        paired = pairing(windows, proposals_count)
        # The two load-bearing properties, true at every ratio.
        assert paired == sorted(paired), (windows, proposals_count)
        if windows >= proposals_count:
            assert set(paired) == set(range(proposals_count)), (windows, proposals_count)
        else:
            assert len(set(paired)) == len(paired), (windows, proposals_count)

    # And the ratio is reachable rather than hypothetical: `populate_windows` clamps a span's
    # count to at least `ceil(span / maximum)` whatever the proposal count, so a 34 s section
    # the model wrote two lines for gets five windows.
    windows = timeline_module.populate_windows(
        [(0.0, 8.0), (8.0, 9.0)], 34.0, maximum=POPULATE_MAX_WINDOW_SECONDS
    )
    assert len(windows) == 5


def test_both_doors_onto_the_snapper_honour_the_same_section_boundaries(tmp_path: Path):
    """One decision, two doors — `snap_cut_plan` sees the boundary line-up sees.

    The layout door is where the defect was found; the timeline door reaches the same core, and a
    boundary protected on a fresh tiling but spendable on a stored one would be the
    two-implementations-of-one-rule failure this codebase keeps meeting.

    `test_one_snapping_core_serves_both_doors`' fixture and its method, with a section layer laid
    over it: same song, same tiling, same tolerance, same band, one door from a project's shots
    and the other from a layout that has no shots at all.
    """
    project = measured_project()
    sections = [
        SongSection(id="section_0", label="Verse", start=0.0, duration=20.0),
        SongSection(id="section_1", label="Chorus", start=20.0, duration=20.0),
        SongSection(id="section_2", label="Outro", start=40.0, duration=20.0),
    ]
    stored = tiled(project)
    stored.sections = sections
    shot_plan = snap_cut_plan(stored, tolerance=0.75, minimum=4.0, maximum=6.0)
    alignment = app_module.line_up_shots(
        layout_of(project, sections=sections), tolerance=0.75, minimum=4.0, maximum=6.0
    )

    # The cuts at 20.0 s and 40.0 s are section boundaries and are refused on both doors, by the
    # same sentence bar the window names — which is what `skip_kind` exists to compare.
    refused = [
        skip.boundary
        for skip in shot_plan.skips
        if skip_kind(skip.reason) == "section-boundary"
    ]
    assert refused == [20.0, 40.0]
    assert [(skip.boundary, skip_kind(skip.reason)) for skip in shot_plan.skips] == [
        (skip.boundary, skip_kind(skip.reason)) for skip in alignment.skips
    ]
    assert [(move.boundary, move.proposed) for move in shot_plan.moves] == [
        (move.boundary, move.proposed) for move in alignment.moves
    ]
    # And with no section layer the very same fixture moves those two cuts, so the refusal above
    # is the sections doing it rather than the geometry.
    unmarked = snap_cut_plan(tiled(project), tolerance=0.75, minimum=4.0, maximum=6.0)
    assert 20.0 in [move.boundary for move in unmarked.moves]
    assert 40.0 in [move.boundary for move in unmarked.moves]


def test_a_line_up_report_carries_the_content_half_and_not_the_manifest(tmp_path: Path):
    """**Defect 4.** 125 KB of report, 211 KB of applied report, and the lyric sheet twice.

    `LineUpResponse.layout` echoed the whole lay-out *confirm* response, and a confirm carries
    `project` — a full manifest copy, lyric sheet included. Harmless in what it produced
    (`layout_from_report` reads the live project) and expensive in every other way: the client had
    to echo a stale manifest byte for byte or the digest refused the confirm, and the nested
    `updated_at` was a revision behind with nothing checking it.

    Trimmed to what fill-in actually reads. The digest still covers all of that, which is the
    second half of this test and the claim that matters.
    """
    client, _store, comfy, _director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    applied = client.post(
        f"/api/projects/{project_id}/timeline/lay-out",
        json={"confirm_replace": True, "plan": layout},
    ).json()
    assert applied["project"] is not None, "the confirm is the response that carried a manifest"

    aligned = line_up(client, project_id, applied)
    assert set(aligned["layout"]) == {"duration", "proposals", "sections"}
    # All three still carry what fill-in divides by, matches on and pairs within.
    assert aligned["layout"]["duration"] == SONG_DURATION
    assert len(aligned["layout"]["proposals"]) == len(applied["proposals"])
    assert [row["label"] for row in aligned["layout"]["sections"]] == [
        label for label, *_ in SECTIONS
    ]
    carried = json.dumps(aligned["layout"])
    assert "Harder faster" not in carried, "the lyric sheet still rides the wire"
    assert '"shots"' not in carried and '"jobs"' not in carried
    # Smaller by a multiple even on this small fixture, where the manifest holds six assets and
    # a four-line sheet; on the Director's own project it was 125 KB against 211 KB.
    assert len(carried) * 3 < len(json.dumps(applied))

    # The half that must not weaken: every field fill-in reads is still inside the digest.
    for edit in (
        lambda plan: plan["layout"]["proposals"][0].__setitem__("prompt", "a substitution"),
        lambda plan: plan["layout"]["sections"][0].__setitem__("label", "Not the intro"),
        lambda plan: plan["layout"].__setitem__("duration", 99.0),
        lambda plan: plan["windows"][0].__setitem__("duration", 9.0),
    ):
        tampered = json.loads(json.dumps(aligned))
        edit(tampered)
        response = client.post(
            f"/api/projects/{project_id}/timeline/fill-in",
            json={"plan": tampered, "confirm_apply": True},
        )
        assert response.status_code == 422, response.text
        assert ProjectStore(tmp_path).get(project_id).shots[0].prompt == ""
    assert comfy.prompts == []


# --------------------------------------------------------------------------------------------
# Phase D — layout variance as a parameter.
#
# The Director's standing complaint: *"shot lengths all look the exact same."* Measured on their
# real song, a freshly populated 30-shot plan had a standard deviation of 0.507 s with **five
# identical 4.308 s windows in a row** through the Verse — the model writes one length per
# section and repeats it, and the tiling had no reason to disagree.
# --------------------------------------------------------------------------------------------

#: The default variance over the word-timed song, both section arms. Different from the neutral
#: digests by construction — a Phase D that reproduced them would have done nothing — and pinned
#: for the same reason those are: the reshaping is arithmetic over one model reply, so it is
#: deterministic, and a change to it is a change to the tiling contract.
#:
#: **Moved 2026-08-23** by the redistribution fix in `timeline._varied_durations`: one window
#: sitting on the band end its density wanted to push it past used to freeze its whole span, and
#: the wider 6.8 s ceiling made that fire in 6 of this fixture's 7 spans — so Phase D was laying
#: a tiling byte-identical to the neutral one and the two assertions below were red. The values
#: they replace are kept beside them, as the neutral pair above keeps theirs. Both were measured
#: at the 6.0 s ceiling and neither survived the ceiling ruling:
#:
#:   marked   a2e805cfd29a0ede2c84d7640931c2f4c8ebf3d8a0aa430bc5326931afe24c06
#:   unmarked 5b545807cf91c99a4b5729adcbd8e584a6b4e589cf4767b823c22928f64965f6
#:
#: **And the move is the redistribution's and nothing else's, checked rather than assumed**, on
#: both arms and against the neutral figures they are pinned against:
#:
#:   coverage        0 → 154.640          unchanged
#:   worst seam      0.0010 s             unchanged, inside `BOUNDARY_TOLERANCE_SECONDS` (20.8 ms)
#:   band            4.000 – 6.800        unchanged, and the band's own ends
#:   window warnings 0                    unchanged (`batch.window_band_note`)
#:   tiling refusals 0                    unchanged (`assembly.tiling_refusals`)
#:   shot count      27                   unchanged — no span's window count is a function of
#:                                        the reshape, which only ever moves seconds between
#:                                        windows that already exist
#:
#: What moves is the shape: within-section stdev 1.003 → **0.902**, and five of the seven spans
#: reshape where none did. The two that do not are the honest last resort rather than the old
#: veto — Verse 2's only above-mean window is already at 4.000 so nobody can give, and Chorus 2's
#: only below-mean window is already at 6.800 so nobody can take. The spread *narrowing* is
#: expected here and is the same point `test_the_default_variance_widens_a_layout_the_model_made
#: _uniform` makes in its docstring: this fixture's proposals already cycle 4–8 s, so aligning
#: the windows to the singing pulls some of them together.
#:
#: **Re-measured on 2026-08-23** with the neutral pair above, for the same reason and with the
#: same proof: `timeline.layout_spans` now absorbs the song's 4.9 ms trailing stub into the Outro
#: span instead of leaving it uncovered, so the Outro's five windows each move by at most a
#: millisecond and nothing else does. The values they replace:
#:
#:   marked   cbd98720b1d0f2f929d230ce76272cb2f5dd6c10ae12e5aca98c983ac32dc87f
#:   unmarked dd923601df5d0f5a36f9677de48a10707350a5eb450cdf6270482c7d69f6ea3d
#:
#: Every figure in the table above still reads the same except coverage, which goes 0 → 154.640
#: to **0 → 154.644**: 27 shots, band 4.000–6.800, worst seam 1 ms, 0 window warnings, 0 tiling
#: refusals.
VARIED_DIGEST_SECTIONS_MARKED = (
    "aae1a1e69d406723ccbb4d2ae8bb0882eebc4a79ed06b539dfe92f36d9e8cea4"
)
VARIED_DIGEST_NO_SECTIONS = (
    "f3eb2656353ea017029b75b6e7664e4641f19118d4ac14511d26c7551391e3d1"
)


@pytest.mark.parametrize(
    "marked,neutral,varied",
    [
        (True, CHAINED_DIGEST_SECTIONS_MARKED, VARIED_DIGEST_SECTIONS_MARKED),
        (False, CHAINED_DIGEST_NO_SECTIONS, VARIED_DIGEST_NO_SECTIONS),
    ],
)
def test_the_default_variance_reshapes_a_word_timed_song_and_stays_legal(
    tmp_path: Path, marked: bool, neutral: str, varied: str
):
    """The feature does something, and everything the tiling promised still holds.

    Contiguous, in band, zero shot-length warnings, and — the constraint Phase D must not
    trample — **no shot straddles a section**: the reshaping happens inside each span, so the
    boundary lay-out tiles per section to protect is still a boundary when it is done.
    """
    client, _store, comfy, _director, project_id = make_client(
        tmp_path, sections=marked, words=True
    )
    body = populate(client, project_id, snap_tolerance=0)
    shots = body["project"]["shots"]
    digest = plan_digest(body["project"])
    assert digest != neutral, "the default variance laid the same windows as the neutral value"
    assert digest == varied
    assert plan_digest(
        ProjectStore(tmp_path).get(project_id).model_dump(mode="json")
    ) == varied

    lengths = [shot["duration"] for shot in shots]
    assert len({round(length, 3) for length in lengths}) > len(lengths) // 2
    cursor = 0.0
    for shot in shots:
        assert abs(shot["start"] - cursor) <= SNAP_CONTIGUITY_TOLERANCE
        assert POPULATE_MAX_WINDOW_SECONDS >= shot["duration"] >= 4.0
        assert window_band_note(Shot(**{k: shot[k] for k in ("start", "duration")})) == ("", "")
        cursor = shot["start"] + shot["duration"]
    # Never past the song — a window 0.1 ms over is a shot `song_audio_window` refuses whole —
    # and short of it by at most the millisecond each span's last window is floored to, which is
    # the tiling's own arithmetic and is what the neutral arm covers as well.
    sections = body["project"]["sections"]
    assert 0 <= SONG_DURATION - cursor <= len(sections) * 1e-3 + 1e-6

    for section in body["project"]["sections"]:
        edge = section["start"] + section["duration"]
        for shot in shots:
            straddles = shot["start"] < edge - SNAP_CONTIGUITY_TOLERANCE and (
                shot["start"] + shot["duration"] > edge + SNAP_CONTIGUITY_TOLERANCE
            )
            assert not straddles, f"{shot} straddles the {section['label']} boundary"
    assert comfy.prompts == []


def test_the_default_variance_widens_a_layout_the_model_made_uniform(tmp_path: Path):
    """The complaint, answered in the one number that states it — against the layout that
    *produces* the complaint.

    The model's measured habit is one length per section, repeated: the live 30-shot plan held
    five identical 4.308 s windows through the Verse and three identical 5.986 s windows through
    the Chorus. This double reproduces exactly that, and the spread grows with the parameter.

    **Deliberately not a claim that variance always widens a spread.** It does not, and the
    ordinary fixture is the counter-example: its proposals already run 4 s to 8 s, so aligning
    the windows to the singing pulls some of them *together*. What the parameter does is make
    length follow the music; a wider spread is what that produces when the music has more to say
    than the model did, which is the case the Director complained about.
    """
    uniform = [
        {
            "start": round(index * 5.0, 3),
            "duration": 5.0,
            "prompt": f"Shot {index}: the performer against the dark.",
            "performance": True,
            "assets": [],
        }
        for index in range(30)
    ]
    spreads = {}
    for variance in (0, 0.5, POPULATE_VARIANCE_DEFAULT):
        client, _store, _comfy, _director, project_id = make_client(
            tmp_path / f"v{variance}",
            director=StepDirector(script=[(uniform, section_rows("u"))]),
            words=True,
        )
        body = populate(client, project_id, snap_tolerance=0, variance=variance)
        lengths = [shot["duration"] for shot in body["project"]["shots"]]
        spreads[variance] = statistics.stdev(lengths)
    # Monotone in the dial, and the default is the top of it — see `POPULATE_VARIANCE_DEFAULT`.
    assert spreads[0] < spreads[0.5] < spreads[POPULATE_VARIANCE_DEFAULT]
    assert POPULATE_VARIANCE_DEFAULT == POPULATE_VARIANCE_MAX


@pytest.mark.parametrize("marked", [True, False])
def test_a_song_nobody_transcribed_is_laid_out_the_same_at_every_variance(
    tmp_path: Path, marked: bool
):
    """**The driver absent.** No word times means no density, and an unmeasured song is
    unmeasured rather than uniformly busy — so the layout is the one this application laid
    before Phase D existed, whatever the parameter says. The same digests the neutral arm
    pins, reached with the default and with the maximum."""
    expected = CHAINED_DIGEST_SECTIONS_MARKED if marked else CHAINED_DIGEST_NO_SECTIONS
    for variance in (POPULATE_VARIANCE_DEFAULT, POPULATE_VARIANCE_MAX):
        client, _store, _comfy, _director, project_id = make_client(
            tmp_path / f"v{variance}", sections=marked, words=False
        )
        body = populate(client, project_id, snap_tolerance=0, variance=variance)
        assert plan_digest(body["project"]) == expected


def test_the_lay_out_report_names_the_variance_it_spent_and_the_digest_covers_it(
    tmp_path: Path,
):
    """An input that shaped the windows has to be readable off the report, or the report cannot
    be accounted for from itself — and it has to be inside `plan_fingerprint`, or a confirm
    could claim a variance the Director never read."""
    client, _store, _comfy, _director, project_id = make_client(tmp_path, words=True)
    report = lay_out(client, project_id, variance=0.25)
    assert report["variance"] == 0.25
    assert report["project"] is None

    tampered = json.loads(json.dumps(report))
    tampered["variance"] = POPULATE_VARIANCE_MAX
    response = client.post(
        f"/api/projects/{project_id}/timeline/lay-out",
        json={"confirm_replace": True, "plan": tampered},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == LAY_OUT_PLAN_MISMATCH
    assert ProjectStore(tmp_path).get(project_id).shots == []

    # And the untampered report still applies, carrying its own number through the confirm.
    applied = client.post(
        f"/api/projects/{project_id}/timeline/lay-out",
        json={"confirm_replace": True, "plan": report},
    ).json()
    assert applied["variance"] == 0.25
    assert [row["duration"] for row in applied["windows"]] == [
        shot.duration for shot in ProjectStore(tmp_path).get(project_id).shots
    ]


def test_variance_reaches_a_layout_with_no_section_layer_at_all(tmp_path: Path):
    """The **other** tiling branch, and the one no fixture reaches by accident.

    `lay_out_shots` tiles per section when there is a section layer and tiles the whole song as
    one span when there is not — and "there is not" needs both halves: the Director marked none
    *and* the shots call volunteered none, because the step adopts whatever structure the model
    hands it. Reached here by scripting the double to return no sections. The variance must be
    spent on that branch too; a parameter honoured on one of two tilings is a parameter that
    works until a model drops a field.
    """
    uniform = [
        {
            "start": round(index * 5.0, 3),
            "duration": 5.0,
            "prompt": f"Shot {index}: the performer against the dark.",
            "performance": True,
            "assets": [],
        }
        for index in range(30)
    ]
    lengths = {}
    bodies = {}
    for variance in (0, POPULATE_VARIANCE_DEFAULT):
        client, _store, _comfy, _director, project_id = make_client(
            tmp_path / f"v{variance}",
            director=StepDirector(script=[(uniform, [])]),
            sections=False,
            words=True,
        )
        body = populate(client, project_id, snap_tolerance=0, variance=variance)
        assert body["project"]["sections"] == [], "this arm needs an empty section layer"
        bodies[variance] = body
        lengths[variance] = [shot["duration"] for shot in body["project"]["shots"]]
    varied = lengths[POPULATE_VARIANCE_DEFAULT]
    # The neutral arm is uniform to within the millisecond the last window is floored by.
    assert max(lengths[0]) - min(lengths[0]) <= 1e-3 + 1e-9
    assert max(varied) - min(varied) > 0.5, "the global branch spent no variance"
    assert 4.0 <= min(varied)
    assert max(varied) <= POPULATE_MAX_WINDOW_SECONDS
    # Total preserved — but asserted against the *tiling's* own precision, not tighter than it.
    # `populate_windows` rounds each duration to the millisecond independently, so a 30-window
    # span's durations can sum up to 15 ms either side of the span whatever the reshaper did:
    # measured, the neutral arm overshoots the 154.644898 s song by 4 ms and the varied one by
    # 6 ms. The reshaper's own zero-sum is exact to 3e-14 and is pinned on the function in
    # `test_timeline.py`; what a *laid* tiling can promise is that the last window ends in the
    # same place, which is the coverage claim and is asserted below.
    assert sum(varied) == pytest.approx(
        sum(lengths[0]), abs=len(varied) * WINDOW_LAY_RESOLUTION / 2
    )
    starts = {
        variance: [shot["start"] for shot in body["project"]["shots"]]
        for variance, body in bodies.items()
    }
    assert starts[0][0] == starts[POPULATE_VARIANCE_DEFAULT][0] == 0.0
    assert (starts[0][-1] + lengths[0][-1]) == pytest.approx(
        starts[POPULATE_VARIANCE_DEFAULT][-1] + varied[-1], abs=1e-9
    ), "the variance moved where the song ends"


def test_a_lay_out_report_round_trips_its_variance_back_into_the_intermediate(tmp_path: Path):
    """`layout_from_report` is one of the four translations between a step's intermediate and
    its wire form, and those are "the only place either shape is built" — so a field that
    travels out has to travel back. Asserted on the function, because nothing downstream of
    line-up reads it and a route test would pass with the field dropped."""
    client, store, _comfy, _director, project_id = make_client(tmp_path, words=True)
    report = lay_out(client, project_id, variance=0.25)
    assert report["variance"] == 0.25
    restored = app_module.layout_from_report(
        store.get(project_id), app_module.LayOutResponse(**report)
    )
    assert restored.variance == 0.25
    assert app_module.layout_report(restored).variance == 0.25


@pytest.mark.parametrize("variance", [-0.1, 1.01, 5])
@pytest.mark.parametrize("route", ["populate", "lay-out"])
def test_a_variance_outside_its_range_is_refused_at_the_edge_not_clamped(
    tmp_path: Path, route: str, variance: float
):
    """Both doors, and 422 rather than a quiet reinterpretation: the parameter's guarantee is
    that H3's band holds, and a request that had to be rewritten to be answerable was a request
    nobody answered. Nothing is written on either path."""
    client, _store, comfy, _director, project_id = make_client(tmp_path, words=True)
    response = client.post(
        f"/api/projects/{project_id}/timeline/{route}",
        json={"confirm_replace": True, "variance": variance},
    )
    assert response.status_code == 422, response.text
    assert ProjectStore(tmp_path).get(project_id).shots == []
    assert comfy.prompts == []
