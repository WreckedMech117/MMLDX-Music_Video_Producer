"""The timeline: snapping its cuts -- and, one day, laying it out and filling it in.

**Two routes, and that is still the finding rather than the plan.** The other six
`/timeline/*` routes -- `compile`, `lay-out`, `line-up`, `fill-in`, `populate` and
`clean-prompts` -- are in `app.py`, each held there by a test that patches a name in
`music_video_producer.app`'s namespace (`lay_out_shots`, `line_up_shots`, `fill_in_shots`,
`plan_fingerprint`, `window_fingerprint`, `citation_fingerprint`, `readiness_report`). A route
defined here would resolve those names against *this* module and never see the patch.
`routes/__init__.py` names each one and the test that holds it.

`snap-targets` came back when the three assertions holding it stopped being keyed to a
filename: two of them find the handler by name anywhere in the package, and the third requires
exactly one module to name the path and that module to declare it with a `GET` and its
response model.

Beside `music_video_producer.timeline`, which is the domain module this one is named after
and never shadows: that one is seconds-to-frames arithmetic, this one is routes. Reach it
from here as `..timeline`.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..app import (
    SNAP_CUTS_NO_SONG,
    SnapCutMove,
    SnapCutSkip,
    SnapCutsRequest,
    SnapCutsResponse,
    SnapTargetsResponse,
    served_measurement,
    shot_render_in_flight,
)
from ..timeline import TimelineError, drag_snap_targets, snap_cut_plan
from .context import RouterContext


def register(ctx: RouterContext) -> None:
    """Register every route this module owns on the application it was handed.

    The context is unpacked into plain locals first -- `app` among them -- so
    every route below is registered by the same decorator, and closes over the
    same names, as it did when it was nested inside `create_app`. The move is
    the whole diff.
    """
    app = ctx.app
    get_project = ctx.get_project
    song_envelope_report = ctx.song_envelope_report
    store = ctx.store

    @app.post(
        "/api/projects/{project_id}/timeline/snap-cuts",
        response_model=SnapCutsResponse,
    )
    def snap_timeline_cuts(
        project_id: str, request: SnapCutsRequest
    ) -> SnapCutsResponse:
        """Move each shot cut to the nearest moment the track leaves voiceless.

        The Director's ruling on the roadmap's long-open "vocal transition points between
        shots" item (2026-08-20): **cut placement is the lever.** Two adjacent references
        shots each perform their own window of the song, so the mouth on A's last frame and
        the mouth on B's first frame come from two calls that never saw each other. Placing
        the cut where nobody is singing removes the mismatch instead of masking it, and costs
        no GPU and no re-render.

        Report first, apply on confirm — `populate`'s `confirm_replace` shape, enforced here
        rather than trusted to the browser. Without `confirm_apply` this route **does not
        call `store.save`**, and the response carries no project at all, so "nothing was
        written" is visible on the wire rather than asserted in prose.

        Every decision is `timeline.snap_cut_plan`'s; this route's only additions are the
        project lookup, the in-flight set (the job records are the evidence, and
        `shot_render_in_flight` is the one reader of them), the honest-empty refusals, and
        the write. Nothing here renders, arms, queues or approves: the shots' windows move
        and every other field on every shot is untouched.
        """
        project = get_project(project_id)
        if not project.song or project.song.duration <= 0:
            raise HTTPException(status_code=422, detail=SNAP_CUTS_NO_SONG)
        rendering = frozenset(
            shot.id for shot in project.shots if shot_render_in_flight(project, shot)
        )
        try:
            plan = snap_cut_plan(
                project, tolerance=request.tolerance, rendering=rendering
            )
        except TimelineError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        # The two honest-empty branches refuse rather than report, because there is nothing
        # to report: no cut was examined. `unmeasured` is the one the codebase's absent-
        # analysis convention is about — an empty `Song.vocal_spans` means *unmeasured, not
        # silent* (`shot_vocal_overlap`), so the alternative to this sentence would be
        # placing every cut in the plan against a silence nobody heard.
        if plan.status in ("unmeasured", "no_cuts"):
            raise HTTPException(status_code=422, detail=plan.message)
        response = SnapCutsResponse(
            applied=False,
            status=plan.status,
            tolerance=plan.tolerance,
            moved=len(plan.moves),
            skipped=len(plan.skips),
            moves=[
                SnapCutMove(
                    before=move.before_label,
                    after=move.after_label,
                    boundary=move.boundary,
                    proposed=move.proposed,
                    shift=move.shift,
                    gap=move.gap,
                    overlap=move.overlap,
                )
                for move in plan.moves
            ],
            skips=[
                SnapCutSkip(
                    before=skip.before_label,
                    after=skip.after_label,
                    boundary=skip.boundary,
                    reason=skip.reason,
                )
                for skip in plan.skips
            ],
            message=plan.message,
        )
        if not request.confirm_apply or not plan.moves:
            return response
        # Applied by shot id from the plan's own `windows`, which is the whole tiling —
        # unchanged shots included — so the contiguity `snap_cut_plan` builds structurally is
        # the contiguity that lands in the manifest, rather than being re-derived here from
        # the moves and given a second chance to drift.
        by_id = {shot.id: shot for shot in project.shots}
        for shot_id, start, duration in plan.windows:
            shot = by_id[shot_id]
            shot.start = start
            shot.duration = duration
        response.project = store.save(project)
        response.applied = True
        return response

    @app.get(
        "/api/projects/{project_id}/timeline/snap-targets",
        response_model=SnapTargetsResponse,
        # **Every field this handler sets is on the wire; nothing it did not set is invented.**
        # The seven below are set unconditionally, so this changes nothing about them — what it
        # protects is the nested envelope, where `served_measurement` carries a missing key as
        # missing on purpose. Without this, the model's `default_factory=list` would turn that
        # into `[]`, which is a measurement of zero beats rather than a measurement that was not
        # taken, and `SnapTargetsEnvelope`'s docstring says why the two must stay apart.
        response_model_exclude_unset=True,
    )
    def read_timeline_snap_targets(project_id: str) -> dict[str, Any]:
        """The song's measurement as the timeline uses it: what to draw and what to land on.

        **One read for one measurement.** This served the drag's targets only, and the band's
        marks came from a second client read of `GET /song/envelope`. Two independent reads of one
        measurement is a drift mechanism, and it drifted three ways, all demonstrated by execution
        in `epic-8-retro-2026-08-24.md`: a byte change under an unchanged manifest record was
        invisible to both keys, so a re-render landing on the same filename left stale marks drawn
        and stale beats snappable until a full reload (S4); one read refused while the other
        succeeded showed the Director no marks while their cut still jumped to a beat, silently
        (S5); and a single project load hashed the master twice and parsed the same 469 KB sidecar
        twice to use 8.8 KB of it (S3). So the two halves are served from one computation here —
        one request, one identity, one failure and one answer — and they cannot describe different
        states because there is nothing for them to disagree about.

        `GET /song/envelope` is **unchanged** and stays the way to read a whole measurement. What
        rides here is `served_measurement`'s projection of it: the marks and the two small arrays
        AD-26's band selector will need, and none of the per-frame series, which is 98.0% of the
        file and is read by nothing in the browser.

        **Why this route rather than the other one.** The gap half comes from a *transcription*
        that has no envelope in it, so this is the read that already had to happen for a song
        nobody analysed; and `snapTargetsIdentity` is `songEnvelopeIdentity` with the word count,
        span count and duration appended, so it is a strict superset and re-reads exactly when
        either half moves.

        **Why this exists at all.** A drag had exactly one target — the playhead. The batch
        "Snap cuts" button had a different and much better one, voiceless gaps, and no drag could
        reach it. Porting the gap rule into the browser was considered and rejected: `timeline.py`
        names a second snapper for a second caller as this codebase's own recurring defect, and a
        drag offering *lyric word edges* while the button clamps into gaps by
        `SNAP_CLEARANCE_SECONDS` would be exactly that — two opinions about where a cut belongs,
        differing by which gesture the Director reached for. So the targets are computed by the
        same module the button uses, served here, and the browser does nothing with them but pick
        the nearest one within its tolerance.

        **Absence of either half is a 200 with the half that exists.** A song nobody transcribed
        has no gap targets and keeps its beats; a song nobody analysed has no beat targets and
        keeps its gaps; a project with no song at all answers two empty lists. None of those is an
        error, and none of them may become one: this is an assist on a gesture that has to keep
        working exactly as it does today whenever a measurement is missing. `song/envelope`'s rule
        verbatim — the only 404 here is the project itself not existing.

        `measured` and `analysed` are reported rather than inferred from the lists being empty,
        because `vocal_gaps` distinguishes *unmeasured* from *measured and voiced throughout* and
        this application's standing convention is that the two are never flattened together.

        **Both are read by the browser and neither is decorative.** They were not, when this route
        shipped, and this docstring said so — which is exactly how the next change comes to assume
        it can stop sending one. `snapSelectorPlan` in `api.js` decides each "Snap to" row's words
        from the flag its kind names in `SNAP_TARGET_EVIDENCE`, and it branches on three values
        rather than two: `true` reads as it always did, `false` says what is missing and (for the
        beats) offers `POST /song/analyze`, and a **missing** key says only that nothing has been
        read yet. So renaming or dropping either field does not degrade the selector, it makes it
        stop describing the song — and the empty lists are not a substitute, for the reason the
        paragraph above gives.

        A sync `def`, so FastAPI runs it in the threadpool: reaching the beats goes through
        `song_envelope_report`, which hashes the whole master to decide whether the measurement is
        still current, and a multi-megabyte read has no business on the event loop.
        """
        project = get_project(project_id)
        report = song_envelope_report(project_id, project)
        served = report.get("envelope") if report.get("present") else None
        # One expression decides both `analysed` and where the beats come from, so the flag cannot
        # disagree with the list it describes. Earlier it was `envelope is not None` while the
        # beats were read behind a separate `isinstance` check, which could answer `analysed: true`
        # with no beats for a report whose envelope was not a mapping, and `analysed: false` for a
        # present report whose envelope key was missing. The flag exists precisely so a reader of
        # the wire can tell those cases apart, so it may not be the thing that blurs them.
        envelope = served if isinstance(served, dict) else None
        beats = envelope.get("beats") or [] if envelope is not None else []
        targets = drag_snap_targets(project.song, beats=beats)
        return {
            "gaps": targets.gaps,
            "beats": targets.beats,
            "measured": targets.measured,
            "analysed": envelope is not None,
            # **Why there is no measurement, in `song_envelope_report`'s own words** — the
            # sentence this route already computed and threw away, and `""` where there is one.
            #
            # It is here rather than behind a second read of `GET /song/envelope` for the reason
            # the `envelope` key below is here: that read hashes the whole master and parses the
            # sidecar again, and two client reads of one measurement is what let the band and the
            # drag describe different states (retrospective S3, S5). One computation, one answer.
            #
            # The band panel is the consumer that makes it necessary rather than merely useful.
            # `analysed: false` tells a Director nothing they can act on, and never-taken,
            # song-changed and sidecar-unreadable are three different remedies (R-11 derives all
            # three at read time, and R-18's rows are the precedent for saying which). The
            # timeline's own "Snap to" rows deliberately do not use it: they are about beats and
            # have their own sentence about beats.
            "reason": str(report.get("reason") or ""),
            "start": targets.start,
            "end": targets.end,
            # The drawing half, off the same `envelope` that `analysed` and the beat targets
            # above it come from. **The whole point of this key is that it is the same object**: the band and the
            # drag used to be two client reads of two routes, each hashing the master and parsing
            # the sidecar for itself, and a byte change under an unchanged manifest record moved
            # one without the other. There is one computation here now, so there is one answer, and
            # `analysed`, `beats` and this cannot describe different states.
            #
            # `null` when there is no measurement, which is `analysed: false` said in the shape the
            # band consumes. Trimmed by `served_measurement` — see `SERVED_ENVELOPE_KEYS` for what
            # is left on disk and why.
            "envelope": served_measurement(envelope),
        }
