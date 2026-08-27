"""The timeline: snapping its cuts -- and, one day, laying it out and filling it in.

**One route, and that is the finding rather than the plan.** The other six `/timeline/*`
routes -- `compile`, `lay-out`, `line-up`, `fill-in`, `populate` and `clean-prompts` -- and
`snap-targets` beside them are all still in `app.py`, each held there by a test that patches
a name in `music_video_producer.app`'s namespace (`lay_out_shots`, `line_up_shots`,
`fill_in_shots`, `plan_fingerprint`, `window_fingerprint`, `citation_fingerprint`,
`readiness_report`) or that reads `app.py`'s source. A route defined here would resolve those
names against *this* module and never see the patch. `routes/__init__.py` names each one and
the test that holds it. This file is kept at one route rather than folded away so the shape
the split was asked for stays visible and the six have somewhere to come back to.

Beside `music_video_producer.timeline`, which is the domain module this one is named after
and never shadows: that one is seconds-to-frames arithmetic, this one is routes. Reach it
from here as `..timeline`.
"""

from __future__ import annotations

from fastapi import HTTPException

from ..app import (
    SNAP_CUTS_NO_SONG,
    SnapCutMove,
    SnapCutSkip,
    SnapCutsRequest,
    SnapCutsResponse,
    shot_render_in_flight,
)
from ..timeline import TimelineError, snap_cut_plan
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
