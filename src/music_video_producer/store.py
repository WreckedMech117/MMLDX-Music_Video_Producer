from __future__ import annotations

import os
import threading
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

#: How the replace backoff waits. A plain `time.sleep` there ran *under* the lock above, so a
#: foreign handle on one manifest stalled every unrelated request in the process — including the
#: two-second `/render-status` poll the lock exists to keep working, and including it on the
#: event loop, because `get` and `save` are called straight from `async def` routes. Waiting on a
#: condition built over the same lock releases it for the duration and re-acquires it afterwards,
#: so contention no longer blocks anyone; each replace *attempt* still happens with the lock held,
#: which is the whole guard. `Condition.wait` releases an `RLock` to its full recursion depth and
#: restores that depth, which matters because `create` nests inside it. Nothing in the application
#: notifies; a test does, to end a backoff on demand instead of on the clock.
_MANIFEST_WAIT = threading.Condition(_MANIFEST_LOCK)

#: Attempts, and the backoff step between them, for the atomic replace. The lock above removes
#: every reader this process owns; these cover the ones it does not — a virus scanner, the
#: search indexer, an editor left open on `project.json`, a second app instance — which hold the
#: destination for a moment and produce the same `WinError 5`. Bounded, and short: a save that
#: cannot land in half a second has hit something that is not going away, and the caller is owed
#: the error rather than a hang.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF = 0.05

#: How many times each manifest has been replaced by this process, keyed by normalised absolute
#: path. Read and written only under `_MANIFEST_LOCK`, which every write in this application
#: holds at the instant of its replace, so the count is exact for this process — the same scope
#: the lock itself claims, and for the same reason (`_MANIFEST_LOCK` cannot see another process
#: either). It exists for one question, asked in `_replace_atomically` after the backoff hands
#: the lock back: *did somebody else land a manifest here while I was waiting?* A counter answers
#: that without touching the disk, which matters because the disk is exactly what is contended in
#: the moment the question is asked. Keyed on `normcase(abspath(...))` rather than on the `Path`,
#: because two `ProjectStore` objects can address one data root by different spellings and two
#: unequal keys would be two writers that cannot see each other.
#:
#: The same counter is handed *out*, by `read_for_update`, as the revision token a caller passes
#: back to `save(..., if_generation=...)`. That is the whole of the compare-and-swap this module
#: offers, and it is free: a read takes the count it read at, a save refuses if the count moved,
#: and neither costs a byte of disk. It is emphatically **not** `Project.updated_at` — that is the
#: client-facing revision the `PUT` routes compare, it lives inside the file, and reading it costs
#: a parse. This one is a fact about the process, with exactly the scope `_MANIFEST_LOCK` has.
_MANIFEST_WRITES: dict[str, int] = {}


def _write_key(target: Path) -> str:
    return os.path.normcase(os.path.abspath(target))


class ProjectNotFound(KeyError):
    pass


class ProjectChangedDuringSave(RuntimeError):
    """Another writer landed a complete manifest while this save was backing off.

    Raised instead of replacing it. The save did **not** happen: nothing of this caller's is on
    disk, and what is there is the other writer's whole manifest, untouched. The caller holds a
    `Project` that was read before that write, so every field of it is potentially stale — which
    is precisely why this cannot be resolved down here. Re-read the project, re-apply the change,
    save again.
    """


def _replace_atomically(temporary_path: Path, target: Path) -> None:
    """Move the finished temp file onto the manifest, retrying a transient Windows lock.

    Call with `_MANIFEST_LOCK` held — the backoff waits on `_MANIFEST_WAIT`, which requires it,
    and every attempt must be made under it or a reader in this process could have the target
    open at the moment of the replace, which is the failure the lock exists to prevent.

    The lock is *dropped across the wait* and taken again for the next attempt, so a save held up
    by a handle this process does not own no longer freezes every other manifest call. What that
    lets in between attempts is another whole `get`, `save` or `list` — never a half one, because
    each of those is itself atomic under the same lock. A reader that slips in opens and closes
    the manifest entirely inside the window and holds nothing by the time this loop re-acquires,
    so the retry cannot be broken by it.

    A *writer* that slips in is the one case the retry may not simply carry on through, and the
    reason this function counts. The temp file was serialised before the wait, so replaying it
    over the newer manifest would revert a save whose caller has already been told it succeeded,
    and would restore that caller's own older `updated_at` — leaving the browser holding a
    revision the server no longer has, refused by the optimistic-concurrency check on its next
    save with its edits already gone. That is the 2026-08-19 defect, where one background save
    reverted thirty-two prompts. "Last writer wins" is the rule for the read/mutate/save that
    every route performs, and it does not extend to here: there, the loser is never told it won.

    So after the wait, if the count for this manifest moved, this save refuses. Re-serialising
    the caller's model instead would fix nothing — the model was read before the other writer
    landed, so fresh bytes of it overwrite exactly the same fields, only now carrying a *newer*
    `updated_at`, which converts a detectable revert into an undetectable one. The staleness is
    in the caller's object and only the caller can resolve it, so the caller is told.

    Failure leaves no debris: a save that gives up removes its own temp file rather than
    accumulating one per attempt inside the project directory, which is what the unlocked
    version did — a few seconds of contention left thousands of them beside the manifest.
    """
    key = _write_key(target)
    generation = _MANIFEST_WRITES.get(key, 0)
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            temporary_path.replace(target)
        except OSError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                temporary_path.unlink(missing_ok=True)
                raise
            _MANIFEST_WAIT.wait(_REPLACE_BACKOFF * (attempt + 1))
            # Checked after the wait rather than before the next attempt's `replace`, so the
            # refusal costs nothing on the ordinary path: with no competing writer the count is
            # unchanged and the loop proceeds exactly as it did before this guard existed.
            if _MANIFEST_WRITES.get(key, 0) != generation:
                temporary_path.unlink(missing_ok=True)
                raise ProjectChangedDuringSave(
                    f"{target.parent.name} was written by another save while this one waited "
                    "for the manifest; re-read the project and re-apply the change"
                )
        else:
            _MANIFEST_WRITES[key] = generation + 1
            return


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

    def save(self, project: Project, *, if_generation: int | None = None) -> Project:
        """Write the whole manifest atomically, or raise. A return is a landed manifest.

        Two failures reach the caller, and both mean nothing of this project was written:
        `OSError` when the destination stayed locked by a handle outside this process for the
        whole bounded retry, and `ProjectChangedDuringSave` when another save landed its own
        complete manifest inside that retry. The second is recoverable and says how: re-read,
        re-apply, save again. Do not swallow it into a success — its whole purpose is that the
        caller who was told 200 keeps what it wrote.

        `if_generation` turns this from "last writer wins" into a compare-and-swap, and is the
        answer to the *other* lost update — the one the backoff guard above cannot see. That
        guard fires only when a foreign handle forces a replace to back off and somebody writes
        during the wait. An ordinary read, mutate, save has no contention on the replace at all:
        it simply lays a manifest read seconds ago over a newer one and raises nothing. That is
        tolerable for a route the Director triggered by clicking — they see the result and can
        act — and intolerable for a background loop, so a background loop passes the generation
        it read at (`read_for_update`) and is refused, in the same exception and with the same
        remedy, when the manifest moved underneath it. Omitted, the behaviour is exactly what it
        always was, which is what every user-initiated route still wants.
        """
        directory = self.project_dir(project.id)
        directory.mkdir(parents=True, exist_ok=True)
        self.media_dir(project.id).mkdir(parents=True, exist_ok=True)
        project.updated_at = now_utc()
        target = self.manifest_path(project.id)
        payload = project.model_dump_json(indent=2)
        # The serialisation above is deliberately outside the lock — it touches no file and is
        # the expensive half. Only the bytes hitting the disk are serialised.
        with _MANIFEST_LOCK:
            # Checked inside the lock and before the temp file, so it is genuinely atomic with
            # the replace it guards — a writer cannot slip between the comparison and the write,
            # because every writer in this process takes this lock to land its bytes.
            if (
                if_generation is not None
                and _MANIFEST_WRITES.get(_write_key(target), 0) != if_generation
            ):
                raise ProjectChangedDuringSave(
                    f"{target.parent.name} was written after this caller read it; re-read "
                    "the project and re-apply the change"
                )
            with NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as temp:
                temp.write(payload)
                temp.flush()
                os.fsync(temp.fileno())
                temporary_path = Path(temp.name)
            _replace_atomically(temporary_path, target)
        return project

    def get(self, project_id: str) -> Project:
        return self.read_for_update(project_id)[0]

    def read_for_update(self, project_id: str) -> tuple[Project, int]:
        """The project, and the write generation it was read at — `save`'s `if_generation`.

        For a caller that intends to write back what it read and cannot afford to lose whatever
        landed in between. The two halves are taken under one acquisition of the lock, which is
        what makes the token honest: a save that lands between them would otherwise be invisible
        to a comparison made afterwards. Costs one dictionary lookup over `get`.

        The token's scope is `_MANIFEST_LOCK`'s scope — this process. A second application
        instance on the same data root is not serialised by either, and is not made safe by this.
        """
        path = self.manifest_path(project_id)
        with _MANIFEST_LOCK:
            if not path.exists():
                raise ProjectNotFound(project_id)
            payload = path.read_text(encoding="utf-8")
            generation = _MANIFEST_WRITES.get(_write_key(path), 0)
        # Parsed after the lock is released: validation is pure CPU on bytes already in hand,
        # and a reader holding the lock through it would block the writes for no gain.
        return Project.model_validate_json(payload), generation

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
