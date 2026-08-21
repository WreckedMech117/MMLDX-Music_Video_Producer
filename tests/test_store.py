import json
from pathlib import Path

import pytest

from music_video_producer.models import Project
from music_video_producer.store import ProjectNotFound, ProjectStore


def test_store_persists_projects_across_instances(tmp_path: Path):
    store = ProjectStore(tmp_path)
    created = store.create(Project(name="Neon Pilgrim"))
    created.creative_brief = "A nocturnal desert pilgrimage"
    store.save(created)

    reopened = ProjectStore(tmp_path).get(created.id)

    assert reopened.name == "Neon Pilgrim"
    assert reopened.creative_brief == "A nocturnal desert pilgrimage"
    assert (tmp_path / "projects" / created.id / "project.json").exists()
    assert (tmp_path / "projects" / created.id / "media").is_dir()


def test_a_manifest_written_before_job_progress_existed_still_loads(tmp_path: Path):
    """The house rule for a new model field: defaulted, so nothing already on disk becomes
    unreadable. Written as the manifest a pre-Phase-4.2 assembly actually left — a `post` job
    with no `progress` key at all — and read back through the real store."""
    store = ProjectStore(tmp_path)
    created = store.create(Project(name="Older manifest"))
    manifest = store.manifest_path(created.id)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["jobs"] = [
        {
            "id": "job_old",
            "kind": "post",
            "status": "complete",
            "prompt_id": "",
            "target_id": "assembly",
            "output_files": ["exports/assembly_00001.mp4"],
            "created_at": payload["created_at"],
            "updated_at": payload["updated_at"],
        }
    ]
    manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    reopened = ProjectStore(tmp_path).get(created.id)

    assert [job.id for job in reopened.jobs] == ["job_old"]
    assert reopened.jobs[0].progress == 0
    # And it round-trips: saving it back adds the key without disturbing anything else.
    store.save(reopened)
    again = json.loads(manifest.read_text(encoding="utf-8"))
    assert again["jobs"][0]["progress"] == 0
    assert again["jobs"][0]["output_files"] == ["exports/assembly_00001.mp4"]


def test_store_lists_newest_project_first(tmp_path: Path):
    store = ProjectStore(tmp_path)
    first = store.create(Project(name="First"))
    second = store.create(Project(name="Second"))

    assert [project.id for project in store.list()] == [second.id, first.id]


def test_store_raises_for_missing_project(tmp_path: Path):
    with pytest.raises(ProjectNotFound):
        ProjectStore(tmp_path).get("missing")


@pytest.mark.parametrize("project_id", ["..", ".", "../outside", "folder/project"])
def test_store_rejects_project_path_traversal(tmp_path: Path, project_id: str):
    store = ProjectStore(tmp_path)
    escaped_manifest = store.projects_root / project_id / "project.json"
    escaped_manifest.parent.mkdir(parents=True, exist_ok=True)
    escaped_manifest.write_text(Project(name="Escaped").model_dump_json(), encoding="utf-8")

    with pytest.raises(ProjectNotFound):
        store.get(project_id)
