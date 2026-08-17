from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import Project, now_utc


class ProjectNotFound(KeyError):
    pass


class ProjectStore:
    """Atomic JSON persistence for standalone production projects."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.projects_root = self.data_root / "projects"
        self.projects_root.mkdir(parents=True, exist_ok=True)

    def project_dir(self, project_id: str) -> Path:
        suffix = project_id.removeprefix("project_")
        if not project_id.startswith("project_") or not suffix.isalnum():
            raise ProjectNotFound(project_id)
        return self.projects_root / project_id

    def media_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "media"

    def manifest_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def create(self, project: Project) -> Project:
        directory = self.project_dir(project.id)
        if directory.exists():
            raise FileExistsError(f"Project already exists: {project.id}")
        self.media_dir(project.id).mkdir(parents=True)
        self.save(project)
        return project

    def save(self, project: Project) -> Project:
        directory = self.project_dir(project.id)
        directory.mkdir(parents=True, exist_ok=True)
        self.media_dir(project.id).mkdir(parents=True, exist_ok=True)
        project.updated_at = now_utc()
        target = self.manifest_path(project.id)
        payload = project.model_dump_json(indent=2)
        with NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as temp:
            temp.write(payload)
            temp.flush()
            os.fsync(temp.fileno())
            temporary_path = Path(temp.name)
        temporary_path.replace(target)
        return project

    def get(self, project_id: str) -> Project:
        path = self.manifest_path(project_id)
        if not path.exists():
            raise ProjectNotFound(project_id)
        return Project.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[Project]:
        projects: list[Project] = []
        for path in self.projects_root.glob("*/project.json"):
            try:
                projects.append(Project.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(projects, key=lambda project: project.created_at, reverse=True)
