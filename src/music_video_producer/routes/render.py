"""Renders: what is submitted, what is queued, and what the machine will hold.

The two job routes are not here. `cancel_open_jobs` is sliced out of `create_app`'s own source
by a test and calls `cancel_job` by name, so both stay in `app.py`; and `read_job` stays with
them because it shares `/jobs/{job_id}` with `cancel_job`, and whichever is registered first
decides the `Allow` header a 405 answers with.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from ..app import (
    RENDER_STATUS_SAVE_ATTEMPTS,
    MusicRequest,
    SamplingProfileRequest,
    SongPlannerRequest,
    VramEjectRequest,
    _require_song_replacement_confirmation,
    _safe_filename,
    logger,
    song_audio_path,
)
from ..batch import (
    PENDING_SUBMISSION_PROMPT_ID,
    RenderStatusReport,
    accept_submission,
    reconcile_render_jobs,
    render_status_report,
)
from ..comfy import ComfyError
from ..models import Project, RenderJob, Song
from ..preferences import EJECT_PREFERENCE_KEY
from ..store import ProjectChangedDuringSave
from ..workflows import (
    build_music3_payload,
    build_songplanner_invented_payload,
    build_songplanner_known_lyrics_payload,
)
from .context import RouterContext


def register(ctx: RouterContext) -> None:
    """Register every route this module owns on the application it was handed.

    The context is unpacked into plain locals first -- `app` among them -- so
    every route below is registered by the same decorator, and closes over the
    same names, as it did when it was nested inside `create_app`. The move is
    the whole diff.
    """
    analyze_a_landed_song = ctx.analyze_a_landed_song
    app = ctx.app
    catalog = ctx.catalog
    comfy = ctx.comfy
    eject_pinned_by_environment = ctx.eject_pinned_by_environment
    ejector = ctx.ejector
    get_project = ctx.get_project
    get_project_for_update = ctx.get_project_for_update
    preferences = ctx.preferences
    record_submission = ctx.record_submission
    render_progress = ctx.render_progress
    settle_unsubmitted_jobs = ctx.settle_unsubmitted_jobs
    store = ctx.store

    def vram_eject_state() -> dict[str, Any]:
        """The setting, where it came from, and what the last eject actually did.

        `enabled` is read off the ejector rather than from any copy, because the ejector's
        own attribute is the thing every submission consults — a second field that could
        disagree with it is a field that will eventually lie.

        `last` carries only what the host itself reported: which models were resident before
        the attempt and which are resident after it. There is deliberately **no free-VRAM
        figure**. Measured on 2026-08-18, the reading fell 31.6 → 16.0 GB across one eject of
        a 4.71 GB model, because ComfyUI released its own cache in the same moment; a number
        that looks like evidence and is not is worse than no number. See `docs/OPERATIONS.md`.
        """
        outcome = getattr(ejector, "last_outcome", None)
        return {
            "enabled": bool(getattr(ejector, "enabled", False)),
            "source": app.state.eject_source,
            "environment_pinned": eject_pinned_by_environment,
            "last": None
            if outcome is None
            else {
                "status": outcome.status.value,
                "detail": outcome.detail,
                "resident_before": list(outcome.resident_before),
                "resident_after": list(outcome.resident_after),
            },
        }

    @app.get("/api/vram-eject")
    def read_vram_eject() -> dict[str, Any]:
        return vram_eject_state()

    @app.put("/api/vram-eject")
    def set_vram_eject(request: VramEjectRequest) -> dict[str, Any]:
        """Turn the eject on or off for every submission route, from now on.

        One assignment, to the one attribute `LlmEjector._attempt` reads on its way in. It
        gates at the `before_submit` funnel rather than at any route, so a submission path
        added tomorrow is covered without knowing this setting exists — and so turning the
        setting *on* adds no code to any render path that could fail one.

        The store is written before the ejector is changed. A choice that cannot be saved is
        refused outright rather than applied for this session only: leaving the setting live
        but unsaved puts a value on screen that silently reverts at the next start, which is
        the same class of lie this feature exists to remove.
        """
        try:
            preferences.set_bool(EJECT_PREFERENCE_KEY, request.enabled)
        except OSError as error:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"The VRAM eject setting could not be saved, so it was not changed: {error}"
                ),
            ) from error
        ejector.enabled = request.enabled
        app.state.eject_source = "director"
        return vram_eject_state()

    @app.put("/api/projects/{project_id}/sampling-profile", response_model=Project)
    def replace_sampling_profile(
        project_id: str, request: SamplingProfileRequest
    ) -> Project:
        """Choose which evidenced H3 bundle this project's reference shots render on.

        The Director's ruling of 2026-08-23, on the 8-step-vs-20-step comparison: turbo is
        "almost sweaty" but "both still look good so **up to user**, and perhaps the video style
        would benefit from it in some cases". Neither bundle is correct, so neither may be a
        silent default — which is what both of them were. `api.generateBatch` sent no profile and
        got 20 steps; `app.js`'s "Render Again" hardcoded `turbo` and got 4. The same project
        rendered two different graphs depending on which button was pressed, and nothing on
        screen named either number. **That** is what this route closes; the ruling is the
        occasion, not the defect.

        **One door, and every render path comes through it.** Nothing else writes the field —
        the generic full-project `PUT` re-adopts the stored value in both directions
        (`replace_default_setting`'s rule, for the reason that route's docstring gives), no tool
        schema exposes it to a model, and `populate` does not touch it. So a bundle change is
        always a thing the Director did, on the control that says what it costs.

        **Nothing here renders, and nothing here changes an existing take.** It is a declaration
        about the next submission. Takes already on disk were rendered on whatever bundle was
        chosen then, and this route does not relabel them — a bundle change re-rolls the take
        rather than re-rendering it better, so a sweep over a plan the Director already has would
        be the silent bulk edit this codebase's report-then-confirm convention forbids.

        **Reference shots only**, which is the field's own scope and not a caveat added here: the
        keyframe and text-only graphs load different checkpoints and have no evidenced bundle, so
        they go on rendering at 20 steps whatever is chosen. `generate_h3` implements exactly
        that, and the control in the browser says it in one clause.
        """
        project = get_project(project_id)
        project.sampling_profile = request.profile
        return store.save(project)

    @app.get("/api/workflows")
    def workflows() -> list[dict[str, Any]]:
        return [
            {
                "id": entry.id,
                "name": entry.name,
                "category": entry.category,
                "relative_path": entry.relative_path,
                "description": entry.description,
                "available": entry.available,
            }
            for entry in catalog.list()
        ]

    @app.post(
        "/api/projects/{project_id}/generate/music",
        response_model=RenderJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def generate_music(project_id: str, request: MusicRequest) -> RenderJob:
        project = get_project(project_id)
        # Before submission: the refusal must cost no GPU time.
        _require_song_replacement_confirmation(project, request.confirm_song_replacement)
        prefix = f"music-video-producer/{project_id}/songs/{_safe_filename(request.title)}"
        payload = build_music3_payload(
            caption=request.caption,
            lyrics=request.lyrics,
            duration=request.duration,
            seed=request.seed,
            prefix=prefix,
        )
        # The record first, then the graph (the Director's 2026-08-21 ruling). A save that
        # loses a race refuses here, before a single byte reaches ComfyUI, so the refusal
        # costs no GPU time — where a save refused *after* the submit answered 409 for a
        # prompt already on the card and lost the only record of it.
        job = RenderJob(
            kind="music",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id="song",
            seed=request.seed,
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project_id, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)
        # **The Song is replaced only once the graph is accepted**, and that is the one thing
        # this route deliberately does *not* move ahead of the submission. Replacing it is
        # destructive — it is why `_require_song_replacement_confirmation` exists — and doing
        # it for a graph ComfyUI then refused would trade a lost job record for a lost song,
        # which is the expensive direction the ruling exists to avoid.
        #
        # Onto a re-read rather than onto `project`, which was read before `comfy.submit` — see
        # `record_submission` for what the stale save costs a second generation running beside
        # this one.
        song = Song(
            title=request.title,
            source="generated",
            duration=request.duration,
            lyrics=request.lyrics,
            caption=request.caption,
            prompt_id=submission.prompt_id,
        )
        # **Deliberately NOT superseded**, and the music routes are the one place a leftover
        # record is left standing on purpose. Every music job shares `target_id="song"` and
        # this route has no per-target in-flight refusal, so two live records here is the
        # easiest state in the application to reach — but the older one cannot do the harm
        # supersession exists to prevent: `apply_job_history` gates song adoption on
        # `Song.prompt_id`, which the assignment above has just replaced, so a late answer to
        # it can never be pasted onto the Song that is now the project's.
        #
        # What it *can* still do is record where its audio landed. Settling it would stop it
        # being reconciled at all, and its `output_files` — the one place an orphaned take is
        # recoverable from, which
        # `test_a_completing_music_job_matches_the_song_by_prompt_id_not_by_source` pins —
        # would stay empty forever. That is a real loss traded for cleanup the three-tick
        # settle already performs. See `batch.supersede_target_jobs`.
        def replace_the_song(fresh: Project) -> None:
            fresh.song = song

        record_submission(project_id, job, patch=replace_the_song)
        return job

    @app.post(
        "/api/projects/{project_id}/generate/songplanner",
        response_model=RenderJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def generate_songplanner(project_id: str, request: SongPlannerRequest) -> RenderJob:
        project = get_project(project_id)
        # Before submission: the refusal must cost no GPU time.
        _require_song_replacement_confirmation(project, request.confirm_song_replacement)
        prefix = f"music-video-producer/{project_id}/songs/{_safe_filename(request.title)}"
        # Before `comfy.submit` for the same reason the confirmation gate is: a duration and
        # headroom whose product leaves `MiniMaxMusic3TextEncode`'s 0.04–360 s schema range
        # would be rejected at `/prompt` validation and reach the Director as an opaque 502.
        # Refused here instead, naming both numbers and the ceiling — never silently clamped,
        # because a quietly shortened ceiling is the very truncation this setting exists to
        # prevent.
        try:
            if request.lyrics is not None:
                payload = build_songplanner_known_lyrics_payload(
                    idea=request.idea,
                    genre_hint=request.genre_hint,
                    lyrics=request.lyrics,
                    duration=request.duration,
                    duration_headroom=request.duration_headroom,
                    seed=request.seed,
                    prefix=prefix,
                )
            else:
                payload = build_songplanner_invented_payload(
                    idea=request.idea,
                    genre_hint=request.genre_hint,
                    duration=request.duration,
                    duration_headroom=request.duration_headroom,
                    seed=request.seed,
                    prefix=prefix,
                )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        # The record first, then the graph, for `generate_music`'s reason and by the same
        # rule: a save race refuses before any GPU time is spent.
        job = RenderJob(
            kind="music",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id="song",
            seed=request.seed,
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project_id, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)
        # And the Song is replaced only once the graph is accepted, for `generate_music`'s
        # reason: the replacement is the destructive act the confirmation gate guards. Onto a
        # re-read for `generate_music`'s other reason — see `record_submission`.
        song = Song(
            title=request.title,
            source="generated",
            duration=request.duration,
            lyrics=request.lyrics or "",
            caption=request.idea,
            prompt_id=submission.prompt_id,
        )
        # Not superseded either, for `generate_music`'s reason and by the same argument: a
        # song planned here and a song generated there are both `kind="music"` on
        # `target_id="song"`, and neither may lose its record of where its audio landed.
        def replace_the_song(fresh: Project) -> None:
            fresh.song = song

        record_submission(project_id, job, patch=replace_the_song)
        return job

    @app.get(
        "/api/projects/{project_id}/render-status", response_model=RenderStatusReport
    )
    async def read_render_status(project_id: str) -> RenderStatusReport:
        """AD-1's poll endpoint: one reconciliation tick, then the fixed report shape.

        A GET the browser calls on a two-second interval while the project has open jobs, so
        every property that matters here is about cost and quiet: an idle project makes no
        ComfyUI request at all, one tick fetches `/queue` once however many jobs are open,
        the manifest is rewritten only when something actually moved, and a dead ComfyUI is a
        200 with `comfy_online: false` rather than a 502 — a poll loop must never turn a
        ComfyUI restart into a toast every two seconds.

        Live percentages ride this same answer. The listener's map is *read* here and nothing
        more: no request is made for it, no branch depends on it, and — the point — it is never
        folded into the project, so `outcome.changed` is exactly what it was before and a tick
        that learned only "the sampler is on step 7" still writes no manifest. A percentage that
        moved `updated_at` twice a second would collide with every optimistic-concurrency check
        the Director's own edits ride on.

        **This tick cannot overwrite anybody.** Its save is a compare-and-swap on the generation
        it read at (`ProjectStore.read_for_update`), because the plain read-mutate-save every
        route performs is a thief when the reader is a loop: this one holds the manifest across
        a `/queue` and a round of `/history`, and anything saved inside that window — a shot
        edit, an approval, a submission stamping its accepted prompt id — would be laid back
        under a two-second-old copy the moment the tick wrote. It cost a real take: the stamp
        reverted, the record kept `PENDING_SUBMISSION_PROMPT_ID`, and the reconciler then
        settled a render that was running on the GPU as never submitted.

        Refused, the tick re-reads and reconciles again rather than re-applying field by field.
        That costs a `/queue` on collision and buys the thing a field list cannot: the verdict is
        always derived from the manifest as it now stands, so `missing_ticks` is incremented from
        the current count exactly once per tick, and nothing here has to be kept in step with
        what `apply_job_history` happens to write this month.

        **What this tick owns**, and the whole of it: each open job's `status`, `error`,
        `missing_ticks` and `output_files`, and what `batch.apply_job_history` writes onto the
        thing a finished job produced — a Shot's `status`, `latest_output` and `latest_review`,
        an Asset's `path`, the Song's `path`. Every one of those is *derived* from ComfyUI's
        answer and authored nowhere else. It owns no shot window, no prompt, no citation, no
        approval, no asset the Director made, no section and no document, so it must never be
        the reason one of those moves — and after this change it cannot be, because the only
        manifest it can write is one it read after the last writer landed.
        """
        project, generation = get_project_for_update(project_id)
        for attempt in range(RENDER_STATUS_SAVE_ATTEMPTS):
            # Inside the loop, not above it: a refused save re-reads the project, so a `before`
            # taken once would be compared against a different object on the second attempt.
            landed_from = song_audio_path(project)
            outcome = await reconcile_render_jobs(project, comfy)
            if not outcome.changed:
                # The commonest tick by far, and the cheapest: nothing moved, so nothing is
                # written and no collision is possible. Left first because it is also the reason
                # the retry below is rare enough to afford. Nothing moved also means the Song's
                # path did not, so the analysis below cannot have been owed on this tick.
                break
            # The other half of "a Song is analysed when it is stored", and the half that covers
            # every generated track. Gated on the path having actually changed, so the ordinary
            # tick — the one that fires every two seconds for the length of a render — does a
            # string comparison and nothing else. **Nothing here may hash the song file on the
            # unchanged path**; that is what this gate is for.
            measured = await analyze_a_landed_song(project_id, project, landed_from)
            try:
                store.save(project, if_generation=generation)
                break
            except ProjectChangedDuringSave:
                # Both sources of the refusal land here and both want the same thing: the
                # generation check above, and the store's older guard against a writer landing
                # inside a replace backoff. Either way this caller's copy is stale and re-reading
                # is the documented remedy.
                if attempt == RENDER_STATUS_SAVE_ATTEMPTS - 1:
                    # The one save in this application that must not become a 409. Refusing it
                    # is right — the alternative is the revert above — but the poll is a loop,
                    # and the next tick two seconds from now re-reads and re-derives exactly
                    # this reconciliation from ComfyUI, so there is nothing to recover and
                    # nothing to tell anyone. Reporting it would put an error toast on the
                    # Director's screen for a race the Director caused by typing. The report
                    # below still describes what the tick learned; only the write was dropped.
                    logger.info(
                        "Render-status tick for %s lost %d save races; the next tick redoes it",
                        project_id,
                        RENDER_STATUS_SAVE_ATTEMPTS,
                    )
                    if measured:
                        # The manifest that would have pointed at this envelope never landed, so
                        # the file is referenced by nothing: this tick's write already replaced
                        # whatever was there, and the pointer that would have named it was
                        # dropped with the save. Removing it leaves the project in the state the
                        # refused save left everything else in, rather than leaving a measurement
                        # on disk that no manifest mentions and nothing will ever clean up.
                        store.song_envelope_path(project_id).unlink(missing_ok=True)
                    break
                project, generation = get_project_for_update(project_id)
        return render_status_report(
            project,
            comfy_online=outcome.comfy_online,
            progress=render_progress.snapshot(),
        )
