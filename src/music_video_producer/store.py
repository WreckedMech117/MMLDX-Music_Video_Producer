from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import Project, now_utc

#: Serialises every manifest read in this process against every manifest write.
#:
#: The write is a temp file plus `os.replace`, which is atomic — but on Windows the replace
#: fails outright with `WinError 5` when *anything* holds the destination open, and an open
#: handle is exactly what a reader is. So the reader breaks the writer: the browser's
#: two-second `/render-status` poll, landing inside a save, makes the save raise, and the save
#: is the half carrying real state (a job settling, an output landing, a take recorded). Every
#: manifest read and write in this application goes through this module, and the lock is on the
#: module rather than on the instance because two `ProjectStore` objects can address one data
#: root — an instance lock would let them race. Reentrant so `create` can call `save` under it;
#: nothing else nests, and the lock is never held across a network call or a callback.
#:
#: In-process is the whole reachable race here: FastAPI runs sync endpoints in a threadpool and
#: async ones on the loop, so `save` from a route thread and `get` from the poll genuinely
#: overlap. A second process on the same data root is not serialised by this — see
#: `_replace_atomically` for what covers handles this process does not own.
_MANIFEST_LOCK = threading.RLock()

#: Attempts, and the backoff step between them, for the atomic replace. The lock above removes
#: every reader this process owns; these cover the ones it does not — a virus scanner, the
#: search indexer, an editor left open on `project.json`, a second app instance — which hold the
#: destination for a moment and produce the same `WinError 5`. Bounded, and short: a save that
#: cannot land in half a second has hit something that is not going away, and the caller is owed
#: the error rather than a hang.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF = 0.05


class ProjectNotFound(KeyError):
    pass


def _replace_atomically(temporary_path: Path, target: Path) -> None:
    """Move the finished temp file onto the manifest, retrying a transient Windows lock.

    Failure leaves no debris: a save that gives up removes its own temp file rather than
    accumulating one per attempt inside the project directory, which is what the unlocked
    version did — a few seconds of contention left thousands of them beside the manifest.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            temporary_path.replace(target)
            return
        except OSError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                temporary_path.unlink(missing_ok=True)
                raise
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))


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
        # Under the lock so the existence check and the write that settles it are one step;
        # `save` re-enters it, which the reentrant lock allows.
        with _MANIFEST_LOCK:
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
        # The serialisation above is deliberately outside the lock — it touches no file and is
        # the expensive half. Only the bytes hitting the disk are serialised.
        with _MANIFEST_LOCK:
            with NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as temp:
                temp.write(payload)
                temp.flush()
                os.fsync(temp.fileno())
                temporary_path = Path(temp.name)
            _replace_atomically(temporary_path, target)
        return project

    def get(self, project_id: str) -> Project:
        path = self.manifest_path(project_id)
        with _MANIFEST_LOCK:
            if not path.exists():
                raise ProjectNotFound(project_id)
            payload = path.read_text(encoding="utf-8")
        # Parsed after the lock is released: validation is pure CPU on bytes already in hand,
        # and a reader holding the lock through it would block the writes for no gain.
        return Project.model_validate_json(payload)

    def list(self) -> list[Project]:
        projects: list[Project] = []
        for path in self.projects_root.glob("*/project.json"):
            # Per manifest rather than around the whole sweep, so listing a data root full of
            # projects never holds a render's save off for the length of the sweep.
            try:
                with _MANIFEST_LOCK:
                    payload = path.read_text(encoding="utf-8")
                projects.append(Project.model_validate_json(payload))
            except (OSError, ValueError):
                continue
        return sorted(projects, key=lambda project: project.created_at, reverse=True)
