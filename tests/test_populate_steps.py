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
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from music_video_producer import app as app_module
from music_video_producer.app import (
    FILL_IN_NO_PLAN,
    FILL_IN_PLAN_MISMATCH,
    FILL_IN_WINDOWS_MOVED,
    LAY_OUT_NO_PLAN,
    LAY_OUT_PLAN_MISMATCH,
    LINE_UP_NO_PLAN,
    POPULATE_CONFIRM_REFUSAL,
    PROJECT_CHANGED_REFUSAL,
    create_app,
)
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


def realistic_project(store: ProjectStore, *, sections: bool = True) -> str:
    """The project every test here populates. Deterministic down to the ids."""
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


def make_client(tmp_path: Path, director=None, *, sections: bool = True):
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    comfy = NullComfy()
    director = director or StepDirector()
    app = create_app(settings=settings, store=store, comfy=comfy, director=director)
    return TestClient(app), store, comfy, director, realistic_project(store, sections=sections)


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

#: Measured on `master` at 9c3db45 — the single-route populate, before the split existed — by
#: running `test_a_chained_populate_writes_exactly_what_the_single_route_wrote` against a
#: stashed tree. Both arms of it are here because the two take different branches: the marked
#: sections tile per section, the unmarked ones tile the whole song as one span and then adopt
#: whatever structure the shots call volunteered.
CHAINED_DIGEST_SECTIONS_MARKED = (
    "8b101523562766bba9abb858498aa79b75f536ffe2f6ff771b59a11b60cab0b8"
)
CHAINED_DIGEST_NO_SECTIONS = (
    "085917f16a42fd9116ba31e8e34952c0c763e36c8d046879787bb6035e3c88c5"
)


@pytest.mark.parametrize(
    "marked,expected",
    [
        (True, CHAINED_DIGEST_SECTIONS_MARKED),
        (False, CHAINED_DIGEST_NO_SECTIONS),
    ],
)
def test_a_chained_populate_writes_exactly_what_the_single_route_wrote(
    tmp_path: Path, marked: bool, expected: str
):
    """**The safety property of the whole change.** Same shots, same windows, same citations,
    same prompts, same `singing`, same seeds, given the same model reply.

    The digest above was taken against `master` before a line of the split was written, by
    running this same body against a stashed tree — not copied off a number somebody wrote
    down. Everything a populate writes is inside it except the shot ids, which are minted
    fresh on every run and could not be identical if they tried.
    """
    client, _store, comfy, _director, project_id = make_client(tmp_path, sections=marked)
    body = populate(client, project_id)

    assert body["proposed"] == 34
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


def line_up(client: TestClient, project_id: str, plan: dict) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/timeline/line-up", json={"plan": plan}
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
    """
    client, _store, comfy, _director, project_id = make_client(tmp_path)
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
    aligned = line_up(client, project_id, layout_applied)
    assert aligned["moved"] == 0 and aligned["measured"] is True
    assert [row["start"] for row in aligned["windows"]] == [
        row["start"] for row in layout_applied["windows"]
    ], "line-up moved a window"
    assert (
        ProjectStore(tmp_path).get(project_id).updated_at == revision
    ), "line-up saved the project"

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
    aligned = line_up(stepped_client, stepped_id, layout)
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


def test_line_up_measures_every_window_and_moves_none(tmp_path: Path):
    """Step two's honest shape: it attaches musical facts and changes no geometry.

    The intro and the outro of this song are measured voiceless, which is the one fact fill-in
    consumes — a window the track leaves silent cannot be marked singing whatever the model
    declared.
    """
    client, _store, comfy, _director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    aligned = line_up(client, project_id, layout)

    assert aligned["moved"] == 0
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
    aligned = line_up(client, project_id, layout_plan)
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


def test_line_up_needs_a_lay_out_report(tmp_path: Path):
    """No plan, an empty plan and an unsigned plan are all "there is nothing to line up"."""
    client, _store, _comfy, _director, project_id = make_client(tmp_path)
    layout = lay_out(client, project_id)
    for body in (
        {},
        {"plan": None},
        {"plan": {**layout, "windows": []}},
        {"plan": {**layout, "plan_id": ""}},
    ):
        response = client.post(
            f"/api/projects/{project_id}/timeline/line-up", json=body
        )
        assert response.status_code == 422, body
        assert response.json()["detail"] == LINE_UP_NO_PLAN, body


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
    aligned = line_up(client, project_id, layout)

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
