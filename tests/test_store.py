import json
import threading
import time
from pathlib import Path

import pytest

from music_video_producer import store as store_module
from music_video_producer.models import Project, Shot
from music_video_producer.store import ProjectChangedDuringSave, ProjectNotFound, ProjectStore


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


def _polled_project(name: str) -> Project:
    """A manifest the size of a real plan, so a read is a window a write can land inside.

    The race is a matter of how long each side holds the file, and a two-field project is over
    too fast to be an honest test of it. Twenty-four shots with prose in them is roughly the
    manifest an overnight batch polls.
    """
    project = Project(name=name)
    project.shots = [
        Shot(start=index * 4.0, duration=4.0, prompt="x" * 600) for index in range(24)
    ]
    return project


def test_a_concurrent_reader_never_makes_a_save_fail(tmp_path: Path):
    """The bug, as it actually bites: the *reader* breaks the *writer*.

    Windows refuses `os.replace` onto a path another handle has open, so a `get` landing inside
    a `save` used to make the save raise `PermissionError` — and a lost save is a lost render
    record, where a lost read is one poll the browser retries two seconds later. Reader threads
    run flat out against a fixed number of saves, which is what a `/render-status` poll does to
    a batch, only faster. Against the unlocked store nearly every save failed; the assertion is
    zero, and it can only flake toward passing, never toward a false failure.

    The leftover check is the same bug's other half: every failed replace abandoned its temp
    file inside the project directory.
    """
    store = ProjectStore(tmp_path)
    project = store.create(_polled_project("Race"))

    stop = threading.Event()
    read_errors: list[Exception] = []
    save_errors: list[Exception] = []
    reads = 0

    def reader() -> None:
        nonlocal reads
        while not stop.is_set():
            try:
                store.get(project.id)
                reads += 1
            except Exception as error:  # noqa: BLE001 - the failure is the measurement
                read_errors.append(error)

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for thread in readers:
        thread.start()
    try:
        for _ in range(60):
            try:
                store.save(project)
            except Exception as error:  # noqa: BLE001 - the failure is the measurement
                save_errors.append(error)
    finally:
        stop.set()
        for thread in readers:
            thread.join(timeout=30)

    assert save_errors == []
    assert read_errors == []
    assert reads > 0, "the readers never ran, so nothing was actually contended"
    leftovers = sorted(
        path.name for path in store.project_dir(project.id).iterdir() if path.is_file()
    )
    assert leftovers == ["project.json"]


def test_a_reader_never_observes_a_half_written_manifest(tmp_path: Path):
    """Atomicity survives the lock: a read returns one whole version or another, never a splice.

    The writer alternates between two names, so any torn or empty read shows up either as a
    validation error or as a name that was never written.
    """
    store = ProjectStore(tmp_path)
    project = store.create(_polled_project("Atomic A"))

    stop = threading.Event()
    seen: set[str] = set()
    failures: list[Exception] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                seen.add(store.get(project.id).name)
            except Exception as error:  # noqa: BLE001 - a torn read is the measurement
                failures.append(error)

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for thread in readers:
        thread.start()
    try:
        for index in range(60):
            project.name = "Atomic A" if index % 2 else "Atomic B"
            store.save(project)
    finally:
        stop.set()
        for thread in readers:
            thread.join(timeout=30)

    assert failures == []
    assert seen, "the readers never ran, so nothing was actually contended"
    assert seen <= {"Atomic A", "Atomic B"}


def test_store_calls_do_not_deadlock_when_nested(tmp_path: Path):
    """No path takes the lock twice and hangs.

    `create` calls `save` under the lock, and routes load, mutate and save in sequence. The
    innermost block is the harshest case — a caller that already holds the lock calling every
    public method — which only a reentrant lock survives. Run in a worker thread so a deadlock
    fails the test on a join timeout instead of hanging the suite.
    """
    store = ProjectStore(tmp_path)
    finished = threading.Event()
    errors: list[Exception] = []

    def work() -> None:
        try:
            created = store.create(Project(name="Nested"))
            loaded = store.get(created.id)
            loaded.creative_brief = "mutated between the read and the write"
            store.save(loaded)
            with store_module._MANIFEST_LOCK:
                store.save(loaded)
                store.get(created.id)
                store.list()
            finished.set()
        except Exception as error:  # noqa: BLE001 - a thread must not lose its own failure
            errors.append(error)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout=60)

    assert errors == []
    assert finished.is_set(), "a nested store call deadlocked"


def test_save_retries_a_replace_a_foreign_handle_blocked(tmp_path: Path, monkeypatch):
    """The lock cannot reach a handle this process does not own — a scanner, a second app.

    Those hold the manifest for a moment and produce the same `WinError 5`, so the replace is
    retried a bounded number of times before it is the caller's problem.
    """
    store = ProjectStore(tmp_path)
    project = store.create(Project(name="Retried"))
    project.creative_brief = "written once the scanner let go"

    real_replace = Path.replace
    attempts: list[Path] = []

    def flaky(self: Path, target: Path) -> None:
        attempts.append(target)
        if len(attempts) <= 2:
            raise PermissionError(5, "Access is denied")
        real_replace(self, target)

    monkeypatch.setattr(store_module, "_REPLACE_BACKOFF", 0.0)
    monkeypatch.setattr(Path, "replace", flaky)
    store.save(project)
    monkeypatch.undo()

    assert len(attempts) == 3
    reopened = ProjectStore(tmp_path).get(project.id)
    assert reopened.creative_brief == "written once the scanner let go"


def test_the_replace_backoff_does_not_hold_the_manifest_lock(tmp_path: Path, monkeypatch):
    """The retry waits for a foreign handle without freezing the rest of the process.

    The backoff used to be a `time.sleep` inside the lock, so up to half a second of waiting on
    a virus scanner blocked every other manifest call — and `get`/`save` are reached straight
    from `async def` routes, so that half second was the event loop, `/render-status` poll
    included. The lock the sleep sat under exists to keep exactly that poll working.

    Asserted on *order*, not on elapsed time: a real reader takes the lock during the backoff and
    records itself, and the lock alone guarantees it lands between the two replace attempts —
    the retry cannot resume until the reader lets go. A version that holds the lock across the
    backoff records the reader last instead, deterministically. The five-second backoff is not a
    measurement; it is only the bound on how long such a version takes to fail.
    """
    store = ProjectStore(tmp_path)
    project = store.create(_polled_project("Backoff"))

    real_replace = Path.replace
    entering_backoff = threading.Event()
    attempts: list[Path] = []
    order: list[str] = []
    reader_errors: list[Exception] = []

    def flaky(self: Path, target: Path) -> None:
        attempts.append(target)
        order.append("replace")
        if len(attempts) == 1:
            entering_backoff.set()
            raise PermissionError(5, "Access is denied")
        real_replace(self, target)

    def reader() -> None:
        try:
            entering_backoff.wait(timeout=30)
            # The poll's own call, not a bare acquire: it must be able to open, read and close
            # the manifest while the retry is waiting.
            store.get(project.id)
            with store_module._MANIFEST_LOCK:
                order.append("reader")
                # Ends the backoff on the reader's terms rather than on the clock.
                store_module._MANIFEST_WAIT.notify_all()
        except Exception as error:  # noqa: BLE001 - a thread must not lose its own failure
            reader_errors.append(error)

    monkeypatch.setattr(store_module, "_REPLACE_BACKOFF", 5.0)
    monkeypatch.setattr(Path, "replace", flaky)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        store.save(project)
    finally:
        monkeypatch.undo()
        thread.join(timeout=60)

    assert reader_errors == []
    assert order == ["replace", "reader", "replace"], (
        "the manifest lock was held across the retry backoff"
    )
    assert ProjectStore(tmp_path).get(project.id).name == "Backoff"
    leftovers = sorted(
        path.name for path in store.project_dir(project.id).iterdir() if path.is_file()
    )
    assert leftovers == ["project.json"]


def test_a_save_that_lands_inside_another_saves_backoff_is_never_reverted(
    tmp_path: Path, monkeypatch
):
    """The lost update the backoff window admits, and the refusal that closes it.

    The retry's temp file was serialised *before* it began waiting. Replaying it over a manifest
    another save landed during the wait reverts a write whose caller already got a 200, and puts
    that caller's older `updated_at` back on disk, so its next save is refused for being stale
    while its edits are the ones that vanished — the 2026-08-19 defect, thirty-two prompts.
    "Last writer wins" is the rule for read/mutate/save and does not stretch to cover this: there,
    the loser is never told it won.

    So the retry refuses. `B`'s manifest is what a reader sees afterwards, byte for byte, with
    `B`'s `updated_at` intact; `A` is told, by a named exception that says what to do about it,
    that nothing of its own was written. Deterministic by ordering, not by timing: the interleaved
    thread starts only once the first replace has failed, and it ends the backoff by notifying the
    condition itself rather than by letting a clock run out — the five-second backoff is only the
    bound on how long a broken version takes to fail.
    """
    store = ProjectStore(tmp_path)
    project = store.create(_polled_project("Interleaved"))
    saver = threading.get_ident()

    real_replace = Path.replace
    entering_backoff = threading.Event()
    attempts: list[int] = []
    order: list[str] = []
    landed: list[str] = []
    other_errors: list[Exception] = []

    def flaky(self: Path, target: Path) -> None:
        attempts.append(threading.get_ident())
        order.append("replace" if threading.get_ident() == saver else "interleaved replace")
        if len(attempts) == 1:
            entering_backoff.set()
            raise PermissionError(5, "Access is denied")
        real_replace(self, target)

    def interleaved() -> None:
        try:
            entering_backoff.wait(timeout=30)
            other = store.get(project.id)
            other.creative_brief = "written inside the other save's backoff"
            store.save(other)
            with store_module._MANIFEST_LOCK:
                # Read raw, under the lock, so this is what is genuinely on disk mid-backoff.
                on_disk = store.manifest_path(project.id).read_text(encoding="utf-8")
                landed.append(json.loads(on_disk)["creative_brief"])
                order.append("interleaved save")
                store_module._MANIFEST_WAIT.notify_all()
        except Exception as error:  # noqa: BLE001 - a thread must not lose its own failure
            other_errors.append(error)

    project.creative_brief = "the retried save"
    monkeypatch.setattr(store_module, "_REPLACE_BACKOFF", 5.0)
    monkeypatch.setattr(Path, "replace", flaky)
    thread = threading.Thread(target=interleaved, daemon=True)
    thread.start()
    try:
        with pytest.raises(ProjectChangedDuringSave):
            store.save(project)
    finally:
        monkeypatch.undo()
        thread.join(timeout=60)

    assert other_errors == []
    # Three entries, not four: the retry never made a second attempt, because it refused first.
    assert order == ["replace", "interleaved replace", "interleaved save"]
    assert landed == ["written inside the other save's backoff"]
    assert ProjectStore(tmp_path).get(project.id).creative_brief == (
        "written inside the other save's backoff"
    )
    leftovers = sorted(
        path.name for path in store.project_dir(project.id).iterdir() if path.is_file()
    )
    assert leftovers == ["project.json"]


def test_a_refused_save_leaves_the_other_writers_updated_at_ahead_of_its_own(
    tmp_path: Path, monkeypatch
):
    """The consequence the refusal exists for, asserted as the client's concurrency check sees it.

    `A` reads at revision one and starts saving. `B` reads the same revision, saves, and is told
    revision two. If `A`'s retry replays its temp file, disk goes back to revision one — and `B`,
    holding two, is refused by `PUT`'s `updated_at` comparison for a change that is already lost.
    The assertion is the ordering that check depends on: what is on disk is never older than the
    newest revision any caller was handed.
    """
    store = ProjectStore(tmp_path)
    project = store.create(_polled_project("Revision"))
    stale = store.get(project.id)

    real_replace = Path.replace
    entering_backoff = threading.Event()
    attempts: list[int] = []
    handed_out: list[str] = []
    other_errors: list[Exception] = []

    def flaky(self: Path, target: Path) -> None:
        attempts.append(target)
        if len(attempts) == 1:
            entering_backoff.set()
            raise PermissionError(5, "Access is denied")
        real_replace(self, target)

    def interleaved() -> None:
        try:
            entering_backoff.wait(timeout=30)
            other = store.get(project.id)
            other.name = "B won"
            handed_out.append(store.save(other).updated_at)
            with store_module._MANIFEST_LOCK:
                store_module._MANIFEST_WAIT.notify_all()
        except Exception as error:  # noqa: BLE001 - a thread must not lose its own failure
            other_errors.append(error)

    stale.name = "A lost"
    monkeypatch.setattr(store_module, "_REPLACE_BACKOFF", 5.0)
    monkeypatch.setattr(Path, "replace", flaky)
    thread = threading.Thread(target=interleaved, daemon=True)
    thread.start()
    try:
        with pytest.raises(ProjectChangedDuringSave):
            store.save(stale)
    finally:
        monkeypatch.undo()
        thread.join(timeout=60)

    assert other_errors == []
    on_disk = json.loads(store.manifest_path(project.id).read_text(encoding="utf-8"))
    assert on_disk["name"] == "B won"
    assert handed_out == [ProjectStore(tmp_path).get(project.id).updated_at], (
        "the revision B was told it had is not the one on disk, so B's next save is refused "
        "for a change that was silently reverted"
    )


def test_two_stores_spelling_one_data_root_differently_still_see_each_others_writes(
    tmp_path: Path, monkeypatch
):
    """The lock is on the module because two `ProjectStore` objects can address one data root.

    The write count that closes the lost-update window has to agree with that: keyed on the path
    as each store happens to spell it, the same manifest becomes two counters and two writers that
    cannot see each other — the refusal never fires and the revert comes straight back. The second
    store here reaches the same directory through a `..` segment, which is the same file and a
    different string.
    """
    (tmp_path / "elsewhere").mkdir()
    store = ProjectStore(tmp_path)
    other_spelling = ProjectStore(tmp_path / "elsewhere" / "..")
    project = store.create(_polled_project("Two spellings"))
    assert other_spelling.get(project.id).name == "Two spellings"

    real_replace = Path.replace
    entering_backoff = threading.Event()
    attempts: list[Path] = []
    other_errors: list[Exception] = []

    def flaky(self: Path, target: Path) -> None:
        attempts.append(target)
        if len(attempts) == 1:
            entering_backoff.set()
            raise PermissionError(5, "Access is denied")
        real_replace(self, target)

    def interleaved() -> None:
        try:
            entering_backoff.wait(timeout=30)
            other = other_spelling.get(project.id)
            other.creative_brief = "written through the other spelling"
            other_spelling.save(other)
            with store_module._MANIFEST_LOCK:
                store_module._MANIFEST_WAIT.notify_all()
        except Exception as error:  # noqa: BLE001 - a thread must not lose its own failure
            other_errors.append(error)

    project.creative_brief = "the retried save"
    monkeypatch.setattr(store_module, "_REPLACE_BACKOFF", 5.0)
    monkeypatch.setattr(Path, "replace", flaky)
    thread = threading.Thread(target=interleaved, daemon=True)
    thread.start()
    try:
        with pytest.raises(ProjectChangedDuringSave):
            store.save(project)
    finally:
        monkeypatch.undo()
        thread.join(timeout=60)

    assert other_errors == []
    assert ProjectStore(tmp_path).get(project.id).creative_brief == (
        "written through the other spelling"
    )


def test_a_backoff_is_not_refused_by_a_save_to_a_different_project(tmp_path: Path, monkeypatch):
    """The guard is per manifest, so an unrelated save is not a reason to refuse.

    A batch saves job records on one project while the Director edits another; those two writes
    touch different files and neither is stale with respect to the other. Refusing on *any*
    concurrent write would turn the busiest ordinary case in this application into an error.
    """
    store = ProjectStore(tmp_path)
    project = store.create(_polled_project("Mine"))
    unrelated = store.create(Project(name="Someone else's"))
    project.creative_brief = "landed after the scanner let go"

    real_replace = Path.replace
    entering_backoff = threading.Event()
    attempts: list[Path] = []
    other_errors: list[Exception] = []

    def flaky(self: Path, target: Path) -> None:
        attempts.append(target)
        if len(attempts) == 1:
            entering_backoff.set()
            raise PermissionError(5, "Access is denied")
        real_replace(self, target)

    def interleaved() -> None:
        try:
            entering_backoff.wait(timeout=30)
            other = store.get(unrelated.id)
            other.creative_brief = "a different manifest entirely"
            store.save(other)
            with store_module._MANIFEST_LOCK:
                store_module._MANIFEST_WAIT.notify_all()
        except Exception as error:  # noqa: BLE001 - a thread must not lose its own failure
            other_errors.append(error)

    monkeypatch.setattr(store_module, "_REPLACE_BACKOFF", 5.0)
    monkeypatch.setattr(Path, "replace", flaky)
    thread = threading.Thread(target=interleaved, daemon=True)
    thread.start()
    try:
        store.save(project)
    finally:
        monkeypatch.undo()
        thread.join(timeout=60)

    assert other_errors == []
    assert ProjectStore(tmp_path).get(project.id).creative_brief == "landed after the scanner let go"
    assert ProjectStore(tmp_path).get(unrelated.id).creative_brief == "a different manifest entirely"


def test_a_backoff_under_a_nested_create_also_releases_the_lock(tmp_path: Path, monkeypatch):
    """`create` calls `save` while already holding the lock, so the release must be recursive.

    A single `release()` would leave the outer acquisition standing and the stall intact — and an
    unbalanced one would corrupt the count. `Condition.wait` drops an `RLock` to its full depth
    and restores that depth, which is why the backoff waits rather than sleeps.
    """
    store = ProjectStore(tmp_path)
    real_replace = Path.replace
    entering_backoff = threading.Event()
    attempts: list[Path] = []
    order: list[str] = []
    reader_errors: list[Exception] = []

    def flaky(self: Path, target: Path) -> None:
        attempts.append(target)
        order.append("replace")
        if len(attempts) == 1:
            entering_backoff.set()
            raise PermissionError(5, "Access is denied")
        real_replace(self, target)

    def reader() -> None:
        try:
            entering_backoff.wait(timeout=30)
            with store_module._MANIFEST_LOCK:
                order.append("reader")
                store_module._MANIFEST_WAIT.notify_all()
        except Exception as error:  # noqa: BLE001 - a thread must not lose its own failure
            reader_errors.append(error)

    monkeypatch.setattr(store_module, "_REPLACE_BACKOFF", 5.0)
    monkeypatch.setattr(Path, "replace", flaky)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        created = store.create(Project(name="Nested backoff"))
    finally:
        monkeypatch.undo()
        thread.join(timeout=60)

    assert reader_errors == []
    assert order == ["replace", "reader", "replace"], (
        "the outer `create` acquisition survived the backoff"
    )
    assert ProjectStore(tmp_path).get(created.id).name == "Nested backoff"
    # The count came back balanced: `create` released both levels on the way out, so a thread
    # that never held it can take it uncontended. An over-release would have raised inside
    # `create` instead; an under-release leaves this acquisition failing.
    released = threading.Event()

    def check_free() -> None:
        if store_module._MANIFEST_LOCK.acquire(blocking=False):
            store_module._MANIFEST_LOCK.release()
            released.set()

    checker = threading.Thread(target=check_free, daemon=True)
    checker.start()
    checker.join(timeout=30)
    assert released.is_set(), "the lock stayed held after a nested create's backoff"


def test_save_that_gives_up_raises_and_leaves_no_temp_file(tmp_path: Path, monkeypatch):
    """A permanently blocked manifest is an error the caller sees, not a hang and not debris."""
    store = ProjectStore(tmp_path)
    project = store.create(Project(name="Blocked"))

    def always_denied(self: Path, target: Path) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(store_module, "_REPLACE_BACKOFF", 0.0)
    monkeypatch.setattr(Path, "replace", always_denied)
    with pytest.raises(PermissionError):
        store.save(project)
    monkeypatch.undo()

    leftovers = sorted(
        path.name for path in store.project_dir(project.id).iterdir() if path.is_file()
    )
    assert leftovers == ["project.json"]
    assert ProjectStore(tmp_path).get(project.id).name == "Blocked"


def test_uncontended_reads_stay_cheap(tmp_path: Path):
    """The poll runs every two seconds and every route reads: the lock must be nearly free.

    An uncontended lock acquisition is nanoseconds, so the bound is deliberately loose — it is
    here to catch a future fix that reaches for a file lock or a sleep on the read path, not to
    measure this machine.
    """
    store = ProjectStore(tmp_path)
    project = store.create(_polled_project("Polled"))

    started = time.perf_counter()
    for _ in range(200):
        store.get(project.id)
    elapsed = time.perf_counter() - started

    assert elapsed < 10.0, f"200 uncontended reads took {elapsed:.2f}s"


@pytest.mark.parametrize("project_id", ["..", ".", "../outside", "folder/project"])
def test_store_rejects_project_path_traversal(tmp_path: Path, project_id: str):
    store = ProjectStore(tmp_path)
    escaped_manifest = store.projects_root / project_id / "project.json"
    escaped_manifest.parent.mkdir(parents=True, exist_ok=True)
    escaped_manifest.write_text(Project(name="Escaped").model_dump_json(), encoding="utf-8")

    with pytest.raises(ProjectNotFound):
        store.get(project_id)
