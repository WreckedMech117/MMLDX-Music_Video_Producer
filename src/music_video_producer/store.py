from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .audio import ENVELOPE_REQUIRED_KEYS, ENVELOPE_VERSION
from .models import Project, now_utc

#: Where a Song Envelope lives, relative to the project directory. Under `media/` because it is
#: derived from the media rather than authored beside the manifest, and in its own `analysis/`
#: directory so a second measurement later (a lyric structure pass, a loudness map) lands beside
#: it rather than loose among songs and takes.
SONG_ENVELOPE_RELATIVE_PATH = "media/analysis/song-envelope.json"

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

#: Computed `Project` fields that answer a question rather than record one, and are therefore
#: served and never saved. `Project.shot_sections` is `section_of`'s verdict about which section
#: each Shot's midpoint lands in; it exists so the browser can target a Section without owning a
#: second copy of that rule, and it is recomputed on every serialisation. Written into the
#: manifest it would be a claim about the relationship between `shots` and `sections` that
#: outlives either of them changing — the same reason no "this effect stack is valid" flag is
#: stored (AD-21). `Shot.end` is stored and is not a counter-example: it restates two fields of
#: the object it sits on.
#:
#: `test_the_steps_add_no_field_to_a_saved_manifest` asserts a saved manifest's keys are a subset
#: of `Project.model_fields`, which computed fields are not in — so that test, not this comment,
#: is what fails if a derived field is added and not listed here.
DERIVED_NOT_STORED = {"shot_sections"}

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
        # `shot_sections` is on the wire and never on the disk. It is `Project.section_of`'s
        # answer recomputed on every serialisation, so storing it would put a derived answer in
        # the file that is this application's source of truth, where it would survive a shot
        # being dragged, a section being remarked, or the manifest being hand-edited, and go on
        # saying what used to be true. `Shot.end` is stored and is not a counter-example: it is
        # a restatement of two fields of the same object, not a claim about the relationship
        # between two lists. `test_the_steps_add_no_field_to_a_saved_manifest` fails if this
        # exclusion is dropped, which is the alarm rather than this comment.
        payload = project.model_dump_json(indent=2, exclude=DERIVED_NOT_STORED)
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

    # ------------------------------------------------------------------
    # Sidecars
    #
    # A sidecar is a derived artefact too large to live in the manifest. There is exactly one
    # today — the Song Envelope — and the rules are `preferences.MachinePreferences`', which is
    # this codebase's only other atomic JSON writer outside the manifest itself:
    #
    #   * The write is a temp file in the destination directory, flushed, fsynced, then
    #     `replace`d. A half-written envelope is never visible under the real name.
    #   * An absent or unreadable file yields *nothing*, never a guessed or zeroed value. The
    #     caller distinguishes "no analysis" from "an analysis of silence", and collapsing the
    #     two would let a corrupt file be served as a measurement.
    #
    # What a sidecar deliberately does **not** get is the manifest's lock, its generation
    # counter, or its `updated_at`. It is not the manifest: nothing polls it, no optimistic
    # concurrency check rides on it, and two writers of it are writing the same measurement of
    # the same bytes. `_replace_atomically`'s whole argument is about not reverting a *save the
    # caller was told succeeded*, and there is no such caller here.
    # ------------------------------------------------------------------

    def song_envelope_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / Path(SONG_ENVELOPE_RELATIVE_PATH)

    def write_song_envelope(self, project_id: str, envelope: dict[str, Any]) -> str:
        """Write one envelope beside the project's media and return its relative path.

        Raises `OSError` if it cannot be written, and `ValueError` if the envelope carries a value
        JSON cannot represent. The caller must not record a `SongAnalysis` pointing at a file that
        is not there — a pointer at nothing reports absent on every read, which is correct but
        wastes the analysis.

        **Nothing is left behind by a failure.** The temp file is created with `delete=False`
        because it has to outlive its own `with` block to be renamed, which means every path out
        of this function owns cleaning it up. The whole write is inside one `try` for that reason:
        a full disk fails in `json.dump`, `flush` or `fsync` rather than in the rename, and those
        are exactly the failures whose remedy this application names ("free some disk") — so
        leaving an orphaned `.tmp` beside the manifest for each attempt would be answering "your
        disk is full" by using more of it.

        `allow_nan=False` because the default is `True` and the default is a trap: `json.dump`
        writes a non-finite float as the bare token `NaN`, which Python reads back happily and
        every strict parser refuses. `audio._finite` already fails the analysis before a value like
        that could get here; this is the second line of the same defence, at the last point where
        it is still cheap to refuse.

        The rename goes through `_replace_atomically`, this module's own answer to the transient
        Windows failure where a virus scanner or an indexer holds the destination for a moment.
        Skipping it meant a completed measurement could be discarded by a lock that would have
        been gone 50 ms later. The manifest lock is taken for the same reason the helper requires
        it — it waits on a condition built over that lock — and for no longer than the rename.
        """
        target = self.song_envelope_path(project_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp"
            ) as temp:
                temporary_path = Path(temp.name)
                json.dump(envelope, temp, separators=(",", ":"), allow_nan=False)
                temp.flush()
                os.fsync(temp.fileno())
            with _MANIFEST_LOCK:
                _replace_atomically(temporary_path, target)
        except BaseException:
            # `BaseException` deliberately: a cancellation or a KeyboardInterrupt mid-write leaves
            # exactly the same orphan an OSError does, and this file is a cache nobody would think
            # to go and sweep.
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        return SONG_ENVELOPE_RELATIVE_PATH

    def read_song_envelope(self, project_id: str, relative_path: str) -> dict[str, Any] | None:
        """The envelope at `relative_path`, or `None` when there is nothing this can read.

        `None` covers every way a sidecar can fail to be an envelope — no path recorded, no file,
        unparseable JSON, JSON that is not an object, an object that is not shaped like an
        envelope, or one written by a version of `audio.py` this build does not know how to read.
        The caller's answer to all of them is the same: report the analysis **absent**.
        Distinguishing them here would invite a caller to treat one as partially usable, and a
        half-read measurement is exactly the "envelope of zeros" this story forbids.

        **"Parses as an object" is not "is an envelope", and the gap is reachable.** The path
        comes off the manifest and the containment check below only proves the file is inside the
        project directory — `project.json` itself passes it. So did `{}`, and both were served as
        `present: true` with no `rms`, no `bands` and no `analysis_frames` for a consumer to index.
        Every key `audio.extract_envelope` promises must be there before this is data.

        The version is checked for the reason `ENVELOPE_VERSION` exists at all: it is bumped when
        the *meaning* of a field changes, and the honest response to a number this build does not
        recognise is to report the analysis absent so it is taken again — never to read last
        year's fields as though they meant what they mean today. It was written and read by
        nothing until this check, which made it decoration.

        The path is contained to the project directory before it is opened. It arrives from a
        manifest, and a manifest is a file a Director can edit; `..` in it must not become a read
        of an arbitrary file on this machine.
        """
        if not relative_path:
            return None
        directory = self.project_dir(project_id).resolve()
        try:
            target = (directory / Path(relative_path)).resolve()
        except OSError:
            return None
        if directory not in target.parents:
            return None
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != ENVELOPE_VERSION:
            return None
        if not ENVELOPE_REQUIRED_KEYS <= payload.keys():
            return None
        return payload
