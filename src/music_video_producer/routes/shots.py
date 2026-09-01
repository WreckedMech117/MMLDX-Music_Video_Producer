"""Shots: the plan's rows, their takes, their effects and the renders they ask for.

Not the two largest: `generate_h3` and `render_shot_preview` -- and, with them, `generate_batch`,
`render_again`, `mark-ready` and `mark-draft` -- are still in `app.py`, held there by tests that
patch `build_h3_reference_payload`, `numbered_references`, `build_effect_stages` and their
neighbours in `music_video_producer.app`'s namespace. `routes/__init__.py` names each one.

`approve`, `unapprove` and `expand-prompt` are here. The first two are the whole of this
application's approval writing: two assignments of `approved_output`, one write of the
`approved` status and four of the window snapshot, and a test scans every module in the
package -- keyed by each module's path, because `Path.name` is not an identity in a package
holding two `timeline.py` -- to keep the count at exactly that.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import HTTPException, status
from fastapi.responses import FileResponse

from ..app import (
    APPROVE_IN_FLIGHT_REFUSAL,
    APPROVE_NO_TAKE_REFUSAL,
    ASSEMBLY_EFFECTS_REFUSAL,
    ASSEMBLY_TRANSITION_REFUSAL,
    BINDING_CARRIER_SHOT,
    BOUNDARY_PREVIEW_NO_NEIGHBOUR_REFUSAL,
    BOUNDARY_PREVIEW_NO_OVERLAP_REFUSAL,
    BOUNDARY_PREVIEW_NO_TRANSITION_REFUSAL,
    BOUNDARY_PREVIEW_REFUSED_BY_PLAN,
    BOUNDARY_PREVIEW_TAKE_MISSING_REFUSAL,
    ENHANCE_IN_FLIGHT_REFUSAL,
    ENHANCE_MISSING_TAKE_REFUSAL,
    ENHANCE_NO_TAKE_REFUSAL,
    ENHANCE_PREFIX_SUFFIX,
    ENHANCE_SINGING_REFUSAL,
    ENHANCE_SINGING_UNKNOWN_REFUSAL,
    EXPAND_PROMPT_LOCKED,
    EXPAND_PROMPT_MALFORMED,
    EXPAND_PROMPT_RENDERED,
    EXPAND_PROMPT_WITHOUT_INTENT,
    EXPAND_PROMPTS_MESSAGE,
    EXPAND_PROMPTS_WITHOUT_SHOTS,
    H3_KEYFRAME_MODES,
    PREVIEW_NO_GEOMETRY_REFUSAL,
    PROJECT_CHANGED_REFUSAL,
    RESTORE_AUDIO_IN_FLIGHT_REFUSAL,
    RESTORE_AUDIO_LENGTH_TOLERANCE,
    RESTORE_AUDIO_MISSING_SONG_REFUSAL,
    RESTORE_AUDIO_MISSING_TAKE_REFUSAL,
    RESTORE_AUDIO_NO_LEAD_REFUSAL,
    RESTORE_AUDIO_NO_SONG_REFUSAL,
    RESTORE_AUDIO_NO_TAKE_REFUSAL,
    RESTORE_AUDIO_NOT_SONG_AUDIO_REFUSAL,
    RESTORE_AUDIO_PREFIX_SUFFIX,
    RESTORE_AUDIO_UNDESCRIBED_TAKE,
    RESTORE_AUDIO_WINDOW_MOVED,
    RESTORE_AUDIO_WINDOW_TOLERANCE,
    SELECT_TAKE_EMPTY,
    SELECT_TAKE_LOCKED,
    SELECT_TAKE_NOT_VIDEO,
    SELECT_TAKE_UNKNOWN,
    SHOT_BINDINGS_ABSENT_REFUSAL,
    SHOT_BINDINGS_CARD_MOVED_REFUSAL,
    SHOT_BINDINGS_NO_SUCH_CARD_REFUSAL,
    SHOT_BINDINGS_UNNAMED_CARD_REFUSAL,
    SHOT_EFFECT_STACK_LIMIT,
    SHOT_EFFECTS_ABSENT_REFUSAL,
    SHOT_EFFECTS_COPY_ONTO_ITSELF_REFUSAL,
    SHOT_EFFECTS_COPY_UNCOMPOSABLE_REFUSAL,
    SHOT_EFFECTS_COPY_UNKNOWN_TARGET_REFUSAL,
    SHOT_EFFECTS_COPY_WITHOUT_TARGETS_REFUSAL,
    SHOT_EFFECTS_LOCKED_REFUSAL,
    SHOT_EFFECTS_TOO_MANY_REFUSAL,
    SHOT_TRANSITION_ABSENT_REFUSAL,
    SHOT_TRANSITION_LOCKED_REFUSAL,
    SHOT_TRANSITION_MIRROR_LOCKED_REFUSAL,
    SHOT_TRANSITION_UNSAID,
    TAKE_MISSING_FILE_REFUSAL,
    TAKE_NOT_RENDERED_REFUSAL,
    UNAPPROVE_NOT_APPROVED_REFUSAL,
    AudioRestoreResponse,
    BoundaryPreviewResponse,
    SelectTakeRequest,
    ShotBindingsRequest,
    ShotDriveBinding,
    ShotDriveResponse,
    ShotEffectsCopyRefusal,
    ShotEffectsCopyRequest,
    ShotEffectsCopyResponse,
    ShotEffectsRequest,
    ShotEffectsResponse,
    ShotExpansionResult,
    ShotListRequest,
    ShotTransitionsRequest,
    ShotTransitionsResponse,
    TransitionCatalogueEntry,
    _adopt_expansion_maps,
    _adopt_shot_effects,
    _adopt_shot_transitions,
    _boundary_is_overlapped,
    _names_an_undiscovered_look,
    _require_approval_unchanged,
    _require_in_flight_status_kept,
    _vision_media,
    adopted_effect_stack,
    apply_expansions,
    assistant_reply,
    attempt_expansion,
    expand_shots,
    expansion_sweep_notices,
    expansion_write_refusal,
    reference_slot_counts,
    refresh_reference_maps,
    shot_audio_restore_in_flight,
    shot_enhancement_in_flight,
    shot_is_approved,
    shot_render_in_flight,
    stack_is_driven,
)
from ..assembly import (
    ASSEMBLY_FPS,
    BOUNDARY_TOLERANCE_SECONDS,
    PREVIEW_PRESET,
    TRANSITION_PREVIEW_MARGIN_FRAMES,
    ClipWindow,
    TransitionChoice,
    TransitionClip,
    clip_frames_on_grid,
    transition_segment_args,
)
from ..batch import PENDING_SUBMISSION_PROMPT_ID, accept_submission, prompt_is_missing, shot_label
from ..comfy import ComfyError
from ..director import DirectorError, DirectorUnavailable
from ..effects import (
    ONE_SIDED_TRANSITION_FRAMES,
    TRANSITION_CATALOGUE,
    TRANSITION_PAIR_ONLY_OPENING_REFUSAL,
    TRANSITION_PAIR_ONLY_REFUSAL,
    EffectRefusal,
    EffectStages,
    boundary_fingerprint,
    build_effect_stages,
    drive_readout,
    transition_definition,
    validate_stack,
)
from ..h3_prompt import check as h3_check
from ..h3_prompt import normalize_audio_fields
from ..models import (
    Project,
    RenderJob,
    VisionInspectionRecord,
    new_id,
    resolve_shot_mode,
    song_audio_tag,
)
from ..reference_map import reference_map_sentence, reference_map_tag_lines
from ..timeline import ordered_shots
from ..workflows import (
    LTX25_ENHANCE_SEED,
    audio_replace_lengths,
    build_audio_replace_payload,
    build_ltx25_enhance_payload,
)
from .context import RouterContext


def register(ctx: RouterContext) -> None:
    """Register every route this module owns on the application it was handed.

    The context is unpacked into plain locals first -- `app` among them -- so
    every route below is registered by the same decorator, and closes over the
    same names, as it did when it was nested inside `create_app`. The move is
    the whole diff.
    """
    app = ctx.app
    comfy = ctx.comfy
    director = ctx.director
    discovered_looks = ctx.discovered_looks
    get_project = ctx.get_project
    get_project_for_update = ctx.get_project_for_update
    record_submission = ctx.record_submission
    resolve_asset_path = ctx.resolve_asset_path
    resolve_song_path = ctx.resolve_song_path
    settings = ctx.settings
    settle_unsubmitted_jobs = ctx.settle_unsubmitted_jobs
    song_envelope_report = ctx.song_envelope_report
    store = ctx.store
    # The preview helpers this module shares with `app.py`'s pinned Shot preview. See
    # `RouterContext` for why they are fields rather than a second copy in here.
    preview_assembly = ctx.preview_assembly
    preview_envelope = ctx.preview_envelope
    preview_into_cache = ctx.preview_into_cache
    preview_side = ctx.preview_side

    @app.put("/api/projects/{project_id}/shots", response_model=Project)
    def replace_shots(project_id: str, request: ShotListRequest) -> Project:
        # Read with the write generation, because the token below is not the whole guard and
        # never was. `updated_at` catches a request built against a revision the server has
        # already moved past — the 2026-08-19 stale tab — and it is compared *here*, before the
        # gates and the adopt helpers below run. Anything that lands between that comparison and
        # the save at the bottom passes it, and this route rewrites the whole manifest: the
        # ledger's own example is `remove_song` detaching a Song inside that window and this save
        # putting it straight back, which no later read can tell from a Director who never
        # detached it. A client that sends no token at all — the wire has always allowed it —
        # has only this. Two guards for two different lies about the same list.
        project, generation = get_project_for_update(project_id)
        # Enforced only when sent — see `ShotListRequest.updated_at`. The wording is
        # `replace_project`'s, because it is the same rule met on the other manifest write.
        if request.updated_at is not None and request.updated_at != project.updated_at:
            raise HTTPException(status_code=409, detail=PROJECT_CHANGED_REFUSAL)
        # The same two gates the whole-project `PUT` carries, on the same argument. This route is
        # the *narrower* sibling and has been the guard hole at least as often, because a client
        # that only wants to move a clip still sends every field of every Shot back.
        _require_in_flight_status_kept(project, request.shots)
        _require_approval_unchanged(project, request.shots)
        # Snapshotted before the assignment below overwrites them: `_adopt_expansion_maps` needs
        # the expansion each shot had, and this route is the narrower sibling that has been the
        # guard hole at least as often as the whole-project one.
        previous = {shot.id: shot for shot in project.shots}
        project.shots = request.shots
        _adopt_expansion_maps(project, previous)
        # And the Effect Stack, on the same snapshot. **This is the sibling that matters most for
        # this field**: dragging a clip, splitting one, or moving a shot's window all write the
        # whole shot list back through here, so a Director who graded ten shots and then nudged
        # one would otherwise lose all ten looks to a gesture that has nothing to do with them.
        # **And it is the route Split and Duplicate land on**, so a Shot this plan does not yet
        # hold keeps the validated stack it arrived with rather than being saved ungraded. See
        # `_adopt_shot_effects`.
        _adopt_shot_effects(project, previous, looks=discovered_looks)
        # And the Transition pair, on the same snapshot and for a sharper version of the same
        # reason. **Dragging a clip is what authors an Overlap**, and dragging writes the whole
        # shot list back through here — so without this guard the single gesture that *makes* the
        # geometry a transition needs would destroy the transition, at 200, every time. See
        # `_adopt_shot_transitions`, including why a Shot this plan does not yet hold gets `None`
        # here where it keeps its Effect Stack.
        _adopt_shot_transitions(project, previous)
        # **This is the route the live defect came in on.** Attach, detach and re-role are all one
        # gesture in the browser — mutate the shot's `citations`, write the whole shot list — so
        # this is where "re-expand automatically when an asset is attached" has to happen. The
        # sweep is over the plan rather than over a diff against the stored shots, for the reason
        # `refresh_reference_maps` gives: this client does not adopt its own reply, so it
        # reasserts the pre-refresh `h3_prompt` on its next gesture and the sweep must catch that
        # too, where a diff would see no citation change and let it stand.
        refresh_reference_maps(project)
        return store.save(project, if_generation=generation)

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/analyze-latest",
        response_model=Project,
    )
    async def analyze_latest_take(project_id: str, shot_id: str) -> Project:
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        output_root = (settings.comfy_root / "output").resolve()
        output = (output_root / Path(shot.latest_output)).resolve()
        if output_root not in output.parents or not output.is_file():
            raise HTTPException(status_code=404, detail="Latest take was not found")
        try:
            image, mime_type = _vision_media(output)
            result = await director.inspect_image(
                image=image,
                mime_type=mime_type,
                purpose=f"generated take for shot {shot.id}; check continuity and reference fidelity",
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (DirectorError, ValueError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        shot.latest_review = VisionInspectionRecord(model=settings.llm_model, **result.model_dump())
        return store.save(project)

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/enhance/ltx25",
        response_model=RenderJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def enhance_with_ltx25(project_id: str, shot_id: str) -> RenderJob:
        """Submit one Shot's existing take to the standalone LTX 2.5 enhancer. No body.

        The gap this closes: LTX 2.5 was reachable only by regenerating H3 from scratch inside
        the reference chain, so improving a shot the Director liked cost another full H3 pass
        and produced a different picture. Here the take is the *input*.

        **Nothing on this path re-runs H3.** The payload has no MiniMax node in it at all — see
        `build_ltx25_enhance_payload`, which the audited export is checked against node by node.

        **Nothing here writes to the Shot.** Not `status`, not `latest_output`, not
        `latest_review`, not `prompt_id`. Only a `RenderJob` is appended. Three consequences,
        and the third is the one that stops the first two from being read as more than they are:

        * the enhanced video is written under `ENHANCE_PREFIX_SUFFIX`, a different filename
          prefix from any render's, so ComfyUI numbers it in its own series and it lands beside
          the take rather than over it or in the middle of it;
        * `read_job` has no branch for `kind="ltx"`, so a *completed* enhancement moves no
          pointer either. The shot goes on naming the take that was enhanced, and the enhanced
          file is reachable through `RenderJob.output_files` on the job that produced it;
        * deciding which of the two is the take is take comparison, which this application does
          not do. None of the above is a take list.

        No body, for `render_again`'s reason and more strongly: this route has no controls at
        all. The export fixes the sigmas, the detailer strength and the prompt, exposing any of
        them is marked Ask First and has not been asked, and a request model with nothing in it
        is a place for a future field to arrive without a decision.

        No readiness gate, and that is deliberate rather than an omission. `generate_h3` refuses
        an unprompted Shot because a prompt is what its graph turns into a picture; this graph's
        prompt is **empty**, so a Shot with no prompt enhances exactly as well as one with a
        prompt. Borrowing that gate here would refuse a real take for a field the work does not
        read. What must exist is the take, which is what the two refusals below check.

        Frame count is not claimed, here or anywhere on this path. The LTX boundary in the
        reference chain measurably did not preserve it (192 in, 185 out), this graph tiles
        temporally, and what it does is a measurement to be taken with `ffprobe` on the output —
        not a number this route can promise.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        # First, ahead of everything, for `mark_ready_refusal`'s reason: an in-flight Shot is the
        # one state where getting this wrong does concrete harm. 409 rather than 422 and for the
        # same reason it is 409 there — a live job is a state conflict, and the same request
        # succeeds once it lands.
        if shot_render_in_flight(project, shot) or shot_enhancement_in_flight(project, shot):
            raise HTTPException(
                status_code=409,
                detail=ENHANCE_IN_FLIGHT_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # The meaning-refusal, ahead of the mechanical ones, on mark-ready's precedent: whether
        # this Shot may be enhanced at all comes before whether its inputs exist. A singing Shot
        # with no take should hear that it is a singing shot — telling it to render first would
        # send the Director to spend GPU on a take this route would then refuse anyway.
        if shot.singing == "singing":
            raise HTTPException(
                status_code=422,
                detail=ENHANCE_SINGING_REFUSAL.format(shot=shot_label(project, shot)),
            )
        if shot.singing == "unknown":
            raise HTTPException(
                status_code=422,
                detail=ENHANCE_SINGING_UNKNOWN_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # Before any path is resolved: a Shot that never rendered has no take to name, and the
        # refusal for that is a different sentence from the one for a take whose file is gone.
        if not shot.latest_output:
            raise HTTPException(
                status_code=422,
                detail=ENHANCE_NO_TAKE_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # `analyze_latest_take`'s resolution, containment check included, so a `latest_output`
        # carrying `..` cannot reach outside ComfyUI's output directory and hand an arbitrary
        # file to the node. The status differs from that route's 404 on purpose: the matrix
        # specifies 422 here, and it is the right code — the request names a Shot that exists,
        # and what cannot be processed is the state its manifest describes.
        output_root = (settings.comfy_root / "output").resolve()
        source = (output_root / Path(shot.latest_output)).resolve()
        if output_root not in source.parents or not source.is_file():
            raise HTTPException(
                status_code=422,
                detail=ENHANCE_MISSING_TAKE_REFUSAL.format(
                    shot=shot_label(project, shot), path=shot.latest_output
                ),
            )
        try:
            payload = build_ltx25_enhance_payload(
                # Forward slashes on Windows too: the value is a plain string to VHS, which
                # opens it with `os.path`, and a backslash path survives the JSON round-trip
                # doubled and unreadable in every log and error message on the way.
                source_video=source.as_posix(),
                prefix=(
                    f"music-video-producer/{project_id}/shots/{shot.id}{ENHANCE_PREFIX_SUFFIX}"
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        # The whole write, and it happens **before** the graph goes out — the Director's
        # 2026-08-21 ruling, for `generate_h3`'s reason: a save race then refuses before any
        # GPU time is spent, where a save refused after the submit answered 409 for a prompt
        # already accepted and lost the only record of the enhancement. The Shot itself is
        # untouched either way: see this route's docstring.
        job = RenderJob(
            kind="ltx",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id=shot.id,
            # The seed the graph fixes, recorded so the job says what was sampled rather than
            # defaulting to a 0 that happens to match.
            seed=LTX25_ENHANCE_SEED,
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project_id, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)
        # Onto a re-read, never onto `project`: this route holds nothing of the target's to
        # write, so the record *is* the write, and laying the pre-submission manifest back over
        # a newer one would revert every unrelated edit made while `/prompt` answered — for the
        # sake of one field on one job. See `record_submission`.
        record_submission(project_id, job)
        return job

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/restore-song-audio",
        response_model=AudioRestoreResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def restore_song_audio(project_id: str, shot_id: str) -> AudioRestoreResponse:
        """Put the master song's own seconds back over one Shot's rendered take. No body.

        The gap this closes, measured on 2026-08-18: **H3 generates its output audio.**
        `VHS_VideoCombine.audio` is fed by a `VAEDecodeAudio` on the sampler's own latent — in
        `build_h3_reference_payload` and in both canonical exports alike — so a song attached
        with `use_song_audio` conditions the lip movement as `ref_audios` and is deliberately
        never the soundtrack. A rendered take correlates with the master at about 0.01 at every
        lag within a second and is 3.4x louder. That is correct, nothing on this path changes
        it, and this route is the stage that was missing: the one that puts the real track back
        over the finished picture.

        **The window is not computed here.** This route hands `build_audio_replace_payload` four
        numbers off the Shot — a window start, a window duration, `song_duration` and the
        recorded `latest_take_lead` — and that builder puts them through the same two functions
        `generate_h3` puts them through. There is no window parameter anywhere on this path for
        the two stages to disagree through, and the failure a second computation would produce —
        a subtle desync rather than an error — has nowhere to come from.

        **The window is the take's, not the shot's** (fixed 2026-08-21). Since the over-render
        margin a take is longer than its window and, below about 3.271 s, centred on it: a
        2.083 s window is 4.4583 s of picture whose first frame is song second `start - 1.2083`.
        This route windowed by the bare `start`/`duration` until that date, which laid the
        exposed slice's seconds over the whole take — the sound running `lead` ahead of the mouth
        and stopping a margin early. It now sends `over_render_frames(duration)` frames of song
        from `start - latest_take_lead`, which is `over_render_window`: the same call, with the
        same lead, that conditioned the render. A shot at 12 s with a 0.25 s lead is restored
        from 11.75 s, and frame 6 of that take is song second 12.000 exactly.

        **And the take's window is the one recorded on the take** (2026-08-21, second pass). The
        paragraph above was the whole fix at first, and it still read the window off the *live*
        `start`/`duration`: correct only until somebody edited them. A take is fixed the moment
        it is submitted; a window is not. Drag a rendered clip's left edge and `start` moves by
        `delta` while `trim_nudge` compensates — the take goes on beginning where it always did —
        so the master was laid `delta` seconds off the lip-sync it was performed against, by a
        frame count the render had not asked for, and both the docstring and
        `AudioRestoreResponse.requested_picture_seconds` called the result "the same count the
        submission sent". `generate_h3` now snapshots the window beside the lead
        (`Shot.latest_take_start`), this route computes from the snapshot, and the claim is true
        by construction rather than by nobody having dragged anything.

        For a take with no snapshot — every take rendered before that date, and every clip
        chosen by hand — the live window is all there is, and the route uses it and **says so**:
        `describes_take` is false and `RESTORE_AUDIO_UNDESCRIBED_TAKE` opens the note. Not
        refused, and the choice is deliberate: nothing distinguishes an unmoved legacy window
        from a moved one, so a refusal would disable this stage for every take that exists today
        over a staleness there is no evidence of, and the harm it would avert is a length
        reported wrongly rather than a sync lost silently — the offset such a take is placed at
        is the one `RESTORE_AUDIO_NO_LEAD_REFUSAL` already refuses to guess.

        The refusal for a window past the end of the song is `song_audio_window`'s, raised inside
        the builder and translated here, so this stage refuses exactly the shots the render
        refuses and in the same words. It is not a second rule. The one refusal this route owns
        beyond the render's is the take with no recorded lead — see
        `RESTORE_AUDIO_NO_LEAD_REFUSAL`, which is a refusal precisely because the alternative is
        a guess about a take's provenance.

        **Nothing here writes to the Shot.** Not `status`, not `latest_output`, not
        `latest_review`, not `prompt_id`. Only a `RenderJob` of `kind="post"` is appended, and
        `read_job` has no branch for that kind, so a *completed* restoration moves no pointer
        either. Three consequences, and the third is the point of the other two:

        * the restored video is written under `RESTORE_AUDIO_PREFIX_SUFFIX`, so ComfyUI numbers
          it in its own series and it lands *beside* the take;
        * the take is opened read-only by `VHS_LoadVideoPath` and is byte-identical afterwards.
          **Its generated audio stays recoverable**, which is not tidiness: hearing "voices but
          no phonetics" in a take is what let the Director find a real conditioning bug on
          2026-08-18, and a pipeline that discards H3's own output discards its best
          diagnostic;
        * deciding which of the two files is *the* take is take comparison, and stitching many
          takes to a master is assembly (FR-22). Both remain unbuilt and neither is presumed
          here.

        This is deliberately a separate act and not something a render does. Applying it
        automatically at render time is marked Ask First in the spec and has not been asked —
        it would remove the ability to hear what H3 actually produced.

        No body, for the enhancer's reason: this route has no controls at all. The window comes
        from the shot, the paths come from the manifest, and the sampling does not exist because
        nothing here samples.

        **No GPU time is spent on any refusal**: every branch below sits ahead of the
        submission. There is very little to spend either way — this payload names zero model
        files and loads no network at all. See `build_audio_replace_payload`.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        # First, for `enhance_with_ltx25`'s reason: an in-flight Shot is the one state where
        # getting this wrong does concrete harm, and 409 rather than 422 because a live job is a
        # state conflict — the same request succeeds once it lands.
        if (
            shot_render_in_flight(project, shot)
            or shot_enhancement_in_flight(project, shot)
            or shot_audio_restore_in_flight(project, shot)
        ):
            raise HTTPException(
                status_code=409,
                detail=RESTORE_AUDIO_IN_FLIGHT_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # Then the take, because a take is what this route's subject *is*: the picture the song
        # goes over. A Shot that never rendered has no take to name, and that is a different
        # sentence from a take whose file is gone.
        if not shot.latest_output:
            raise HTTPException(
                status_code=422,
                detail=RESTORE_AUDIO_NO_TAKE_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # `enhance_with_ltx25`'s resolution, containment check included, so a `latest_output`
        # carrying `..` cannot reach outside ComfyUI's output directory and hand an arbitrary
        # file to the node.
        output_root = (settings.comfy_root / "output").resolve()
        source = (output_root / Path(shot.latest_output)).resolve()
        if output_root not in source.parents or not source.is_file():
            raise HTTPException(
                status_code=422,
                detail=RESTORE_AUDIO_MISSING_TAKE_REFUSAL.format(
                    shot=shot_label(project, shot), path=shot.latest_output
                ),
            )
        # Then whether this shot has a window at all. Before the song is resolved, because a
        # shot that never rode the master is refused for that whether or not a song exists —
        # telling such a Director to add a song would send them to fix the wrong thing.
        if not shot.use_song_audio:
            raise HTTPException(
                status_code=422,
                detail=RESTORE_AUDIO_NOT_SONG_AUDIO_REFUSAL.format(
                    shot=shot_label(project, shot)
                ),
            )
        if not project.song or not project.song.path:
            raise HTTPException(
                status_code=422,
                detail=RESTORE_AUDIO_NO_SONG_REFUSAL.format(shot=shot_label(project, shot)),
            )
        try:
            song = resolve_song_path(project_id, project.song)
        except HTTPException as error:
            # `resolve_song_path` answers 404 for "the media is not there", which is right for
            # a media route and wrong here: the request names a project and a Shot that both
            # exist, and what cannot be processed is the state the manifest describes. Re-raised
            # as the matrix's 422, naming the recorded path so a moved file is distinguishable
            # from a cleared directory.
            raise HTTPException(
                status_code=422,
                detail=RESTORE_AUDIO_MISSING_SONG_REFUSAL.format(
                    shot=shot_label(project, shot), path=project.song.path
                ),
            ) from error
        # The take's own window, before anything is computed from a window at all. A
        # `latest_take_duration` of 0 is "never snapshotted" — the model constrains a real
        # `duration` to `gt=0` — and it is what every take rendered before 2026-08-21 reads, and
        # every clip `select_shot_clip` cleared the bookkeeping for. Described takes compute from
        # the take; undescribed ones fall back to the live window and the response says which of
        # the two it was. See `Shot.latest_take_start` and `RESTORE_AUDIO_UNDESCRIBED_TAKE`.
        describes_take = shot.latest_take_duration > 0
        take_start = shot.latest_take_start if describes_take else shot.start
        take_duration = shot.latest_take_duration if describes_take else shot.duration
        # Only askable of a described take: an undescribed one has nothing to compare the live
        # window against, which is exactly why it cannot be claimed to describe anything.
        window_moved = describes_take and (
            abs(take_start - shot.start) > RESTORE_AUDIO_WINDOW_TOLERANCE
            or abs(take_duration - shot.duration) > RESTORE_AUDIO_WINDOW_TOLERANCE
        )
        # The take's own bookkeeping, last of the refusals and still before any submission. See
        # `RESTORE_AUDIO_NO_LEAD_REFUSAL`: a take that begins past 0 s and records no lead is a
        # take this route cannot place, and placing it anyway is the guess the whole stage
        # refuses to make elsewhere. Asked of the take's window rather than the shot's, so a
        # rendered shot dragged to 0 s is still refused for the take it actually holds.
        if take_start > 0 and not shot.latest_take_lead:
            raise HTTPException(
                status_code=422,
                detail=RESTORE_AUDIO_NO_LEAD_REFUSAL.format(
                    shot=shot_label(project, shot), start=take_start
                ),
            )
        try:
            payload = build_audio_replace_payload(
                # Forward slashes on Windows too, for `enhance_with_ltx25`'s reason: the value
                # is a plain string to VHS, and a backslash path survives the JSON round-trip
                # doubled and unreadable in every log and error message on the way.
                source_video=source.as_posix(),
                source_audio=song.as_posix(),
                # The four numbers, unmodified. Everything correct about this stage follows from
                # these going to `song_audio_window` and `over_render_window` rather than to a
                # window computed here. All four describe the take rather than the plan:
                # `latest_take_lead` is read off the Shot and never recomputed, because
                # `over_render_lead` would answer for the take a submission *now* would produce;
                # and the window is the one recorded with that lead, for the same reason one step
                # further out — the live `start`/`duration` are a different pair the moment
                # anybody drags the clip.
                start=take_start,
                duration=take_duration,
                song_duration=project.song.duration,
                take_lead=shot.latest_take_lead,
                prefix=(
                    f"music-video-producer/{project_id}/shots/"
                    f"{shot.id}{RESTORE_AUDIO_PREFIX_SUFFIX}"
                ),
            )
        except ValueError as error:
            # Covers the window-past-the-end refusal, raised by `song_audio_window` inside the
            # builder, and every path-shape refusal beside it. Before `comfy.submit`, so none of
            # them costs anything.
            raise HTTPException(status_code=422, detail=str(error)) from error
        # The same four numbers again, so what the Director is told about the take is the take
        # the payload above carries rather than a second description of it.
        lengths = audio_replace_lengths(
            start=take_start,
            duration=take_duration,
            song_duration=project.song.duration,
            take_lead=shot.latest_take_lead,
        )
        # The whole write, and it happens **before** the graph goes out — the Director's
        # 2026-08-21 ruling, and this is the route that found the defect: a save refused after
        # the submit answered 409 for a graph already queued, and the restored file landed on
        # disk with no record of it anywhere. The Shot itself is untouched either way: see
        # this route's docstring.
        #
        # `prompt_id` is `PENDING_SUBMISSION_PROMPT_ID` in the window and deliberately **not**
        # the empty string, which on a `kind="post"` record already means something else
        # entirely — local ffmpeg work, which the assemble route's busy check, startup healing
        # and `api.js`'s progress branch all key on. See that constant.
        job = RenderJob(
            kind="post",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id=shot.id,
            # No sampling happens here, so there is no seed. Left at the model's 0 rather than
            # borrowed from the shot, which would record a number nothing used.
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project_id, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)
        # Onto a re-read, never onto `project`: this route holds nothing of the target's to
        # write, so the record *is* the write, and laying the pre-submission manifest back over
        # a newer one would revert every unrelated edit made while `/prompt` answered — for the
        # sake of one field on one job. See `record_submission`.
        record_submission(project_id, job)
        matched = (
            abs(lengths["requested_picture_seconds"] - lengths["audio_seconds"])
            <= RESTORE_AUDIO_LENGTH_TOLERANCE
        )
        return AudioRestoreResponse(
            job=job,
            audio_seconds=lengths["audio_seconds"],
            requested_picture_seconds=lengths["requested_picture_seconds"],
            requested_frames=int(lengths["requested_frames"]),
            lengths_match=matched,
            describes_take=describes_take,
            length_note=(
                f"{lengths['audio_seconds']:g}s of the master song, from "
                f"{take_start - shot.latest_take_lead:g}s to "
                f"{take_start - shot.latest_take_lead + lengths['audio_seconds']:g}s, over a "
                f"picture the render asked H3 for as {int(lengths['requested_frames'])} frames "
                f"({lengths['requested_picture_seconds']:.4g}s at 24 fps). "
                + (
                    "The two agree. "
                    if matched
                    # The one way the two numbers *this route computed* can differ, and it is
                    # stated about them rather than about the file: `over_render_window`'s only
                    # clamp is the song's own end, so a shortfall is the song running out before
                    # the requested picture would have reached. Whether the file on disk holds
                    # that many frames is a separate claim and the sentence below refuses to
                    # make it — which is what keeps this branch honest for an undescribed take,
                    # where the requested count is the plan's rather than the render's.
                    else "The two differ: the master runs out before the requested picture "
                    "does, so the tail of the take keeps its own audio. "
                )
                + (RESTORE_AUDIO_UNDESCRIBED_TAKE if not describes_take else "")
                + (
                    RESTORE_AUDIO_WINDOW_MOVED.format(
                        start=shot.start,
                        duration=shot.duration,
                        take_start=take_start,
                        take_duration=take_duration,
                    )
                    if window_moved
                    else ""
                )
                + "Neither is padded or cut: trim_to_audio is off. The frames the file "
                "actually holds are an ffprobe reading, not a number this application claims."
            ),
        )

    @app.get(
        "/api/projects/{project_id}/shots/{shot_id}/effects",
        response_model=ShotEffectsResponse,
    )
    def read_shot_effects(project_id: str, shot_id: str) -> ShotEffectsResponse:
        """One Shot's Effect Stack. `[]` for a Shot that carries none, which is not an error.

        A read of the manifest and nothing else — no validation, no catalogue lookup, no verdict
        about whether the stack still composes. That verdict belongs to the moment of composing
        (AD-21): `build_effect_stages` re-derives it at export and refuses by name, and a stored
        "this is valid" flag is the thing this codebase refuses to keep, because it can outlive
        its condition — a look deleted from the folder by a Director who never opened this project
        would leave the flag still saying yes.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        return ShotEffectsResponse(shot_id=shot.id, effects=shot.effects)

    @app.put(
        "/api/projects/{project_id}/shots/{shot_id}/effects", response_model=Project
    )
    def replace_shot_effects(
        project_id: str, shot_id: str, request: ShotEffectsRequest
    ) -> Project:
        """Write one Shot's Effect Stack — validated against the catalogue before a byte is stored.

        **The one route that changes an existing Shot's stack**, which is what keeps a look out of
        reach of everything that is not a Director: the two generic manifest writes re-adopt the
        stored stack for every Shot they already hold (`_adopt_shot_effects`), no tool schema
        declares it, and the Director's context withholds it. A look is an eye on a take.

        The one thing that arrives elsewhere is a Shot that is *new* to the plan, which those
        writes keep — validated by the same `validate_stack` this route runs — because Split and
        Duplicate copy the look onto a new id and save the whole list. Nothing there can move a
        stack that already exists; see `_adopt_shot_effects` for why the halves of one shot have
        to grade alike.

        The order of the three gates below is the order they have to be in. The Shot is found
        first, so a request naming nothing is a 404 rather than a lecture about locks. The lock is
        next, because it is a decision the Director made and it holds whatever the body says —
        `shot_write_refusal` uses the same precedence for the same reason, and FX-7's other half
        (drawing the controls disabled) is C2's; a guard that lives only in the interface is not
        one.

        **422 for the lock, with `render_again`, `mark_ready` and `select_take`.** The Director
        settled that split on 2026-08-18 and `mark_ready` carries the reasoning: 409 is for a
        *live render*, where the same request succeeds once the render lands and nothing about
        it is unprocessable. A lock is not that. It is a fact about the Shot that no amount of
        waiting changes — it clears by a deliberate act, never by patience — and that ruling
        names `locked` on the 422 side by name. This slice's spec asked for 409; keeping it
        would have reopened the drift the ruling exists to close.

        Validation is last and is `effects.validate_stack`, which owns the catalogue and is
        the same function the export runs again, so this route and the chain cannot come to
        different verdicts about one stack.

        **Nothing is stored until every one of them passes.** The stack is assigned onto the Shot
        after the refusal can no longer be raised, and `store.save` is the last statement — so a
        422 or a 409 leaves the manifest untouched, which is what "nothing was composed" has to
        mean at a route as well as in the composer.

        A 422 carries the refusal's own sentence, whole. Those sentences name the offending
        effect, parameter and bound and were written to be read by a Director; slice B asserts
        them verbatim, and paraphrasing one here would be a second wording of the same refusal.

        The looks are resolved only for a stack that has something in it. An empty stack — the
        common write, since clearing every card is how a Director takes a look back off — cannot
        name a LUT, so it costs no folder read at all.

        **A body naming no `effects` at all is refused rather than read as an empty stack.** See
        `ShotEffectsRequest.effects` and `SHOT_EFFECTS_ABSENT_REFUSAL`: `{"efects": [...]}` used
        to answer 200 and erase the grade. `{"effects": []}` is untouched by that and stays the
        way every card comes off.

        **The stack is capped before it is validated** (`SHOT_EFFECT_STACK_LIMIT`), because the
        chain becomes one `-vf` argument and an argv over 32767 characters fails on Windows as
        "ffmpeg is not installed" — a false diagnosis of a fault a client caused. The bound is
        checked here rather than at the export so the refusal names the write that grew it.

        **It writes the stack and never a Parameter Binding.** A binding lives inside an
        `EffectSpec` and is minted only by `replace_shot_bindings` below (AD-16). Every card here
        is matched to the stored card of the same id and **adopts that card's bindings**
        (`adopted_effect_stack`), so this route can go on being what the panel writes on every
        slider drag, card toggle and Story 9.4 reorder without the Director's own gesture
        destroying their own work — and a body cannot invent, alter, relocate or drop a binding,
        because nothing it says about one is read.

        **The wire contract that costs, said where a reader of the route meets it (R-33).** A
        client writing a bound Shot's stack must echo the card ids it read. The panel does, through
        `api.effectStackWrite`; a hand-rolled client that does not is refused by name rather than
        losing a binding to a 200, because losing one is otherwise indistinguishable from removing
        its card. A Shot that holds no binding is untouched by any of this and writes exactly as it
        always did, ids or no ids.

        **A look the catalogue does not know rescans the folder once and asks again.** A Director
        who drops `brand-new-look.cube` in and immediately grades with it was told "There is no
        look called 'brand-new-look' in the looks folder" — a sentence naming the folder as the
        authority when the authority was a process-lifetime cache, and offering no remedy
        reachable from this route. That is the expected gesture, not an exotic one. Exactly one
        rescan, and only for that refusal: this must not become a folder read per request (221 ms
        cold on the Director's 44 MB pack), and every other refusal is a fact about the body that
        no amount of re-reading the disk can change.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        if shot.locked:
            # 422, with `render_again` and `mark_ready`, and not 409. The Director settled that
            # split on 2026-08-18 and `mark_ready` carries the reasoning: 409 is for a *live
            # render*, where the same request succeeds once the render lands and nothing about it
            # is unprocessable. A lock is not that. It is a fact about the Shot that no amount of
            # waiting changes — it clears by a deliberate act, never by patience — which is the
            # line that ruling drew, and it names `locked` on the 422 side by name.
            raise HTTPException(
                status_code=422,
                detail=SHOT_EFFECTS_LOCKED_REFUSAL.format(shot=shot_label(project, shot)),
            )
        if request.effects is None:
            raise HTTPException(
                status_code=422,
                detail=SHOT_EFFECTS_ABSENT_REFUSAL.format(shot=shot_label(project, shot)),
            )
        if len(request.effects) > SHOT_EFFECT_STACK_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=SHOT_EFFECTS_TOO_MANY_REFUSAL.format(
                    limit=SHOT_EFFECT_STACK_LIMIT, count=len(request.effects)
                ),
            )
        try:
            validate_stack(
                request.effects, luts=discovered_looks() if request.effects else ()
            )
        except EffectRefusal as refusal:
            if not _names_an_undiscovered_look(refusal):
                raise HTTPException(status_code=422, detail=str(refusal)) from refusal
            # The one retry, and the only refusal that earns one: a look this process has not
            # discovered *yet*. One rescan, then the answer stands whatever it is.
            try:
                validate_stack(request.effects, luts=discovered_looks(rescan=True))
            except EffectRefusal as rescanned:
                raise HTTPException(status_code=422, detail=str(rescanned)) from rescanned
        # Every card's bindings come off the stored card of that id, whatever the body says about
        # them (R-33). This is the route the effects panel writes on every slider drag, every
        # reorder and every card toggle, and it must be able to do all three without touching a
        # binding — in either direction. A body that invents one is writing filter automation
        # through a route that never asked the Director for a band, a drive or a depth; a body
        # that drops one has the Director's own gesture destroy their own work at 200; and a body
        # that moves one between two cards of one effect changes the rendered picture at 200,
        # which is what the multiset this replaced could not see (A3).
        #
        # `own` and never `elsewhere`: a card belonging to some other Shot is not this Shot's to
        # write back, so a body naming one is handed a new card with a new id and no bindings.
        # Carrying a look from Shot to Shot is `POST .../effects/copy`, which says so out loud.
        #
        # What is left, and it is the one thing an id cannot decide: a client that names *some*
        # ids and leaves a bound card's id out is read at its word, because a card whose id is
        # absent is a card this write does not have — which is also exactly how a card is removed.
        # A client naming *no* ids at all on a bound Shot is refused by name instead
        # (`SHOT_EFFECTS_WITHOUT_CARD_IDS_REFUSAL`), because that one is not a gesture, it is a
        # client that has never heard of the field.
        adoption = adopted_effect_stack(
            request.effects, own=shot.effects, source=BINDING_CARRIER_SHOT
        )
        if adoption.refusal:
            raise HTTPException(status_code=422, detail=adoption.refusal)
        shot.effects = adoption.stack
        return store.save(project)

    @app.get(
        "/api/projects/{project_id}/shots/{shot_id}/transitions",
        response_model=ShotTransitionsResponse,
    )
    def read_shot_transitions(project_id: str, shot_id: str) -> ShotTransitionsResponse:
        """One Shot's Transition pair, and the twelve this application offers.

        A read of the manifest plus a constant — no validation, no verdict about whether the
        stored type still exists, `read_shot_effects`' rule and AD-21's: the verdict belongs to
        the moment of composing, where `_transition_catalogue_refusals` re-derives it and refuses
        by name. A stored "this is valid" flag is the thing this codebase refuses to keep.

        **The catalogue rides along** because story 11.1's own last acceptance criterion is that
        the route is sufficient without any interface, and a client that can set a transition but
        cannot discover the vocabulary is not sufficient. It costs no disk read.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        return ShotTransitionsResponse(
            shot_id=shot.id,
            transition_out=shot.transition_out,
            transition_in=shot.transition_in,
            catalogue=[
                TransitionCatalogueEntry(
                    transition_id=entry.transition_id,
                    label=entry.label,
                    xfade=entry.xfade,
                    pair_only=entry.pair_only,
                    # The ceiling, not the length that will run: the export clamps it to the
                    # clip's own frames and records what it clamped to. `None` where there is no
                    # one-sided form to have a length (story 11.4).
                    one_sided_frames=(
                        None if entry.pair_only else ONE_SIDED_TRANSITION_FRAMES
                    ),
                )
                for entry in TRANSITION_CATALOGUE.values()
            ],
        )

    @app.put(
        "/api/projects/{project_id}/shots/{shot_id}/transitions",
        response_model=Project,
    )
    def replace_shot_transitions(
        project_id: str, shot_id: str, request: ShotTransitionsRequest
    ) -> Project:
        """Write one Shot's Transition — validated against the catalogue before a byte is stored.

        **The one route that writes either field** (AD-16), which is what keeps a transition out of
        reach of everything that is not a Director: the two generic manifest writes re-adopt the
        stored pair for every Shot they hold and give a new Shot none
        (`app._adopt_shot_transitions`), no tool schema declares either field, and the Director's
        context withholds both. Set a type to author a blend; send `null` to clear it. That is the
        whole of what story 11.1 needs, with no interface at all.

        **The mirror is kept in step here** (AD-30). `transition_out` on the earlier Shot is
        authoritative and is the only side the export builds a boundary's picture from -- R-45
        reads the incoming field at the plan's first frame and nowhere else. Writing it also
        writes the *following* Shot's `transition_in`, so a panel drawn on either Shot shows one
        blend rather than two halves that can disagree. Following in **song order**, `timeline.ordered_shots`', because
        that is the order `assembly_plan` resolves and a mirror written to the neighbour in list
        order would name a different Shot than the export blends. Writing `transition_in` directly
        mirrors backwards the same way, so a client may write either end.

        The order of the gates is `replace_shot_effects`': the Shot first, so a request naming
        nothing is a 404 rather than a lecture about locks; the lock next, because it is a decision
        the Director made and it holds whatever the body says (422, on the 2026-08-18 ruling that
        names `locked` on the unprocessable side); then the body's own shape; then the catalogue,
        which owns which transitions exist and is the same function the export runs again.

        **A pair-only type is refused where there is no Overlap to move two pictures across**
        (FX-19, R-34) — the reason such an entry is in the catalogue at all rather than absent
        from it. **In two sentences since 2026-08-31**, because the ordinary one names a remedy
        that does not exist at the one boundary with no Shot on the other side of it: a
        `transition_in` on the Shot nothing plays before is the video's own opening, and no drag
        can put a picture there (`TRANSITION_PAIR_ONLY_OPENING_REFUSAL`, R-45). Judged against
        the geometry as it stands at the write, which is when the Director is choosing; it is necessary and not sufficient, because dragging the clip apart afterwards
        is FX-16's own case and story 11.4's, and nothing here pretends otherwise. What it does
        buy is that a Director who picks "Wipe left" on a boundary with no Overlap is told why, at
        the moment they pick it, instead of watching an export do nothing.

        **Nothing is stored until every gate passes**, and `store.save` is the last statement — so
        a 422 leaves the manifest untouched, exactly as the sibling route does.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        if shot.locked:
            raise HTTPException(
                status_code=422,
                detail=SHOT_TRANSITION_LOCKED_REFUSAL.format(
                    shot=shot_label(project, shot)
                ),
            )
        said = {
            side: value
            for side, value in (
                ("transition_out", request.transition_out),
                ("transition_in", request.transition_in),
            )
            if value is not SHOT_TRANSITION_UNSAID
        }
        if not said:
            raise HTTPException(
                status_code=422,
                detail=SHOT_TRANSITION_ABSENT_REFUSAL.format(
                    shot=shot_label(project, shot)
                ),
            )
        ordered = ordered_shots(project)
        position = next(
            (spot for spot, item in enumerate(ordered) if item.id == shot.id), None
        )
        for side, value in said.items():
            if value is None:
                continue
            try:
                entry = transition_definition(value.type)
            except EffectRefusal as refusal:
                raise HTTPException(status_code=422, detail=str(refusal)) from refusal
            # The pair-only check, against the boundary this side names: `transition_out` is the
            # seam with the Shot that follows, `transition_in` the seam with the one before.
            neighbour = None
            if position is not None:
                if side == "transition_out" and position + 1 < len(ordered):
                    neighbour = (shot, ordered[position + 1])
                elif side == "transition_in" and position > 0:
                    neighbour = (ordered[position - 1], shot)
            overlapping = (
                neighbour is not None
                and neighbour[0].end - neighbour[1].start > BOUNDARY_TOLERANCE_SECONDS
            )
            if entry.pair_only and not overlapping:
                # **Which of the two refusals, and it is one condition** (R-45, story 11.f8). The
                # sentence above tells a Director to drag the two clips across each other, and at
                # the one boundary with no Shot on the other side of it there is nothing to drag:
                # a `transition_in` on the Shot with nothing before it is the video's own opening,
                # which no edit of this timeline can give a second picture. The export says the
                # same sentence at the same boundary (`_compose_opening_transition`), because two
                # wordings for one condition teach a Director the application holds two opinions.
                wording = (
                    TRANSITION_PAIR_ONLY_OPENING_REFUSAL
                    if side == "transition_in" and neighbour is None
                    else TRANSITION_PAIR_ONLY_REFUSAL
                )
                raise HTTPException(
                    status_code=422,
                    detail=wording.format(
                        label=entry.label,
                        shot=shot_label(project, shot),
                        # The side the addressed boundary is on: a `transition_in`'s
                        # boundary is the seam *before* its Shot. The opening wording
                        # names no neighbour and ignores this (`str.format` drops an
                        # unused key), which is why one call serves both sentences.
                        neighbour="before" if side == "transition_in" else "after",
                        alternatives=", ".join(
                            sorted(
                                item.label
                                for item in TRANSITION_CATALOGUE.values()
                                if not item.pair_only
                            )
                        ),
                    ),
                )
        # **The lock, on the other end of the blend** (2026-08-30). The gate above holds the
        # addressed Shot; this holds the Shot the mirror would write. It is judged over the same
        # `said` sides and the same neighbour arithmetic the write loop below uses -- one walk,
        # written twice, would be two answers to "which Shot does this touch" and that is the
        # shape the frame rule was just corrected for.
        #
        # It covers a clear as well as a set: `null` on `transition_out` un-authors the locked
        # successor's `transition_in` exactly as a type authors it, and a lock that held the
        # writing but not the erasing would be no lock.
        #
        # **And it holds a blend, not a Shot's treatment of its own frames** (2026-08-31). It
        # shipped without asking whether the two Shots overlap at all, so a lock anywhere made the
        # Shot in front of it un-fadeable: `shot_one[0, 4.0] shot_two[4.0, 8.5]`, `shot_two`
        # locked, `transition_out: fade_black` on `shot_one` answered 422 saying *"a transition
        # between SHOT 01 and SHOT 02 is written on both of them"* -- and there is no transition
        # between them. On a boundary with no Overlap a `transition_out` is a **one-sided**
        # treatment of the addressed Shot's own last frames (AD-19, story 11.4); the only thing
        # written on the locked Shot is a `transition_in` the export reads at no boundary but
        # the plan's own first frame (R-45), which is not this one: the Shot the mirror writes
        # has the addressed Shot in front of it, so it never opens. So the gate asks
        # `_boundary_is_overlapped`, which is `assembly._paired_transitions`' own arithmetic and
        # the same question the pair-only check above asks: a lock on the neighbour holds a blend.
        for side in said:
            if position is None:
                continue
            mirrored = None
            if side == "transition_out" and _boundary_is_overlapped(ordered, position):
                mirrored = ordered[position + 1]
            elif side == "transition_in" and position > 0 and _boundary_is_overlapped(
                ordered, position - 1
            ):
                mirrored = ordered[position - 1]
            if mirrored is None or not mirrored.locked:
                continue
            earlier, later = (
                (shot, mirrored) if side == "transition_out" else (mirrored, shot)
            )
            raise HTTPException(
                status_code=422,
                detail=SHOT_TRANSITION_MIRROR_LOCKED_REFUSAL.format(
                    shot=shot_label(project, mirrored),
                    before=shot_label(project, earlier),
                    after=shot_label(project, later),
                ),
            )
        # Written after every refusal can no longer be raised, and both sides of one blend are
        # written together whichever end the client named (AD-30).
        for side, value in said.items():
            setattr(shot, side, value)
            if position is None:
                continue
            if side == "transition_out" and position + 1 < len(ordered):
                ordered[position + 1].transition_in = value
            elif side == "transition_in" and position > 0:
                ordered[position - 1].transition_out = value
        return store.save(project)


    def _margin_frames(plan, spot: int, shot_id: str) -> int:
        """How many frames of one Shot's own clip sit beside a transition entry at `spot`.

        The ceiling is `TRANSITION_PREVIEW_MARGIN_FRAMES` and the floor is what the Shot actually
        has there. **Read off `plan.frames`, which is the only source that has already survived
        `assembly_refusals`** -- so a leg is never asked for frames its take does not hold, which
        is the negative-trim failure `take_cut_refusal` exists for and which ffmpeg answers at
        rc 0 with a picture of the wrong seconds.

        `0` for a neighbour that is not this Shot's own `ClipWindow`: a transition entry beside
        another transition entry, or beside a third Shot's clip, has nothing of *this* Shot to
        show. The boundary is still previewable -- the blend itself is always there -- it simply
        has no lead or no tail on that side.
        """
        if spot < 0 or spot >= len(plan.clips):
            return 0
        entry = plan.clips[spot]
        if not isinstance(entry, ClipWindow) or entry.shot_id != shot_id:
            return 0
        return max(0, min(TRANSITION_PREVIEW_MARGIN_FRAMES, plan.frames[spot]))

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/boundary-preview",
        response_model=BoundaryPreviewResponse,
    )
    async def render_boundary_preview(
        project_id: str, shot_id: str
    ) -> BoundaryPreviewResponse:
        """One boundary, previewed: the outgoing Shot, the blend and the incoming Shot as **one
        continuous piece**, at half the export's size (FX-21, story 11.5).

        **Its own route and its own key, which is R-35** and is the whole shape of this story.
        `effects.preview_fingerprint` takes one take and one window and its docstring asserts as an
        invariant that a preview is *"never one half of a resolved overlap"* -- which this is
        exactly two of. Widening it was rejected on a measurement rather than on taste: an input
        added there that does not canonicalise to nothing when absent renames every cached clip in
        every project on merge day, and Epic 10 already paid for the version of that where the
        client's key and the server's fingerprint disagreed and the Monitor went on showing a
        picture driven by a song the project no longer had.

        **Named by the outgoing Shot**, because AD-30 makes `transition_out` on the earlier Shot
        authoritative and the later Shot's `transition_in` a mirror that decides nothing *at a
        boundary between two Shots*. A route addressed by the incoming side would be addressed by
        the field this boundary's picture is never built from -- R-45 reads that field at the
        plan's first entry and nowhere else, and this route refuses that end by name for having no
        neighbour at all (`BOUNDARY_PREVIEW_NO_NEIGHBOUR_REFUSAL`).

        **The blend is the export's own, and it is the export's own by construction rather than by
        care.** The plan comes from `preview_assembly`, which calls `assembly_plan` -- so which
        boundaries became a `TransitionClip` at all, how many frames the blend is, and which take
        seconds each leg reads are the export's answers, not a second arithmetic for the same
        question. The argv comes from `assembly.transition_segment_args`, the export's own
        builder, with `lead_frames` and `tail_frames` that default to nothing there; and the
        `xfade` clause is written by `assembly.xfade_stage` for both, so *"the transition previewed
        is the export's, by name and by duration"* (FX-NFR-3) is a string comparison rather than a
        reading of two builders.

        **Both legs compose their own Shot's effects, in their own leg namespace** (R-41). Two
        graded Shots blending ungraded pictures would not match the clips on either side of the
        seam, and without the prefix both legs -- which each start at chain slot 0 -- would emit
        duplicate filtergraph labels and, for two bound Shots, one `sendcmd` target driving both,
        which is silent at rc 0.

        **The absence says which absence it is.** Five states have no blend to look at and each
        has its own sentence: nothing follows this Shot; the two Shots do not overlap, so the
        transition is one-sided and this Shot's *own* preview is the picture of it; they overlap
        with nothing chosen, so the boundary is a hard cut; the plan refused the geometry (R-37),
        reported in the plan's own words; or one of the two takes could not be read.

        **One manifest read, and no re-read after the probes.** `render_shot_preview` re-reads,
        because its window and its stack are read separately and a slider moved in between would
        name a clip after a look nobody is looking at. Here the plan *is* the read: the windows
        the probes were taken against and the stacks the chains are composed from come out of one
        manifest, so the name describes the picture that was rendered. A change landing during the
        probes is answered by the next request, under a key that has moved.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        label = shot_label(project, shot)
        ordered = ordered_shots(project)
        position = next(
            (spot for spot, item in enumerate(ordered) if item.id == shot.id), None
        )
        if position is None or position + 1 >= len(ordered):
            raise HTTPException(
                status_code=422,
                detail=BOUNDARY_PREVIEW_NO_NEIGHBOUR_REFUSAL.format(shot=label),
            )
        after = ordered[position + 1]
        after_label = shot_label(project, after)
        # The one arithmetic three other places already use -- `assembly._paired_transitions`,
        # `app._compose_one_sided_transitions` and `replace_shot_transitions` -- rather than a
        # fourth spelling of "do these two overlap".
        if not _boundary_is_overlapped(ordered, position):
            raise HTTPException(
                status_code=422,
                detail=BOUNDARY_PREVIEW_NO_OVERLAP_REFUSAL.format(
                    before=label, after=after_label
                ),
            )
        stored = shot.transition_out.type if shot.transition_out else ""
        if not stored:
            raise HTTPException(
                status_code=422,
                detail=BOUNDARY_PREVIEW_NO_TRANSITION_REFUSAL.format(
                    before=label, after=after_label
                ),
            )
        try:
            entry = transition_definition(stored)
        except EffectRefusal as refusal:
            raise HTTPException(
                status_code=422,
                detail=ASSEMBLY_TRANSITION_REFUSAL.format(shot=label, detail=refusal),
            ) from refusal
        # Every boundary the catalogue agrees to, so the plan below is the plan the export builds
        # rather than a plan holding one transition. It matters for the frame counts: an Overlap
        # earlier in the song splits a clip this boundary's lead is measured against.
        #
        # A type the catalogue does not know is **skipped here and refused above**, and the two
        # are not the same rule read twice. The export refuses outright over an unknown type
        # anywhere in the project (`_transition_catalogue_refusals`); refusing to *look at* a
        # perfectly good boundary because some other Shot holds a bad value would make a Director
        # unable to see the transition they are being asked to fix.
        choices: dict[str, TransitionChoice] = {}
        for item in ordered:
            if not item.transition_out:
                continue
            try:
                known = transition_definition(item.transition_out.type)
            except EffectRefusal:
                continue
            choices[item.id] = TransitionChoice(
                transition_id=known.transition_id, xfade=known.xfade
            )
        plan = await preview_assembly(project, choices)
        if plan is None:
            raise HTTPException(
                status_code=422, detail=PREVIEW_NO_GEOMETRY_REFUSAL.format(shot=label)
            )
        # **Both Shots, not just the addressed one** (2026-08-30). `position` and `after` are read
        # off `ordered_shots`, which holds *every* Shot; the plan comes from `preview_assembly`,
        # which leaves out any Shot whose take cannot be resolved or measured. So the two lists
        # are not the same list, and matching on `before.shot_id` alone bound this route to a
        # `TransitionClip` whose `after` was a **different Shot** -- reproduced: the response named
        # `shot_b`, the frames showed `shot_a` blending into `shot_c`, and `tail_frames` collapsed
        # to 0 because `_margin_frames` found no `shot_b` clip beside the entry.
        #
        # The mismatch has exactly one cause, which is why it needs no sentence of its own: pairs
        # in the plan are consecutive among the Shots the plan holds, so a plan pairing `shot_a`
        # with something other than its own successor is a plan the successor's take never reached.
        # `BOUNDARY_PREVIEW_TAKE_MISSING_REFUSAL` below says that, and it is the fifth of the five
        # absences this route's docstring enumerates rather than a sixth.
        #
        # **That claim was true of the design and false of the code until 2026-08-31.** The
        # refusal lookup below matched a *substring*, so this route reached that sentence only
        # when no refused boundary in the whole plan happened to name this Shot -- and the case
        # this comment is about, a Shot the plan drops, is exactly the case where a neighbouring
        # boundary's refusal does name it. Selecting by the pair is what makes the sentence
        # reachable, which is what makes this paragraph true.
        index = next(
            (
                spot
                for spot, item in enumerate(plan.clips)
                if isinstance(item, TransitionClip)
                and item.before.shot_id == shot.id
                and item.after.shot_id == after.id
            ),
            None,
        )
        if index is None:
            # R-37's refusals, whole. The plan says why a boundary the manifest asked to blend
            # stayed a hard cut, and that sentence is the one the export records on
            # `ExportLook.transitions` -- reworded here it would be this application holding two
            # opinions about one geometry. Nothing matching means the pair is not in the plan at
            # all, which is a take that could not be read.
            #
            # **Selected by the pair, which is the same correction `66c90d8` made to the index
            # lookup twelve lines above and did not make here** (2026-08-31). This read
            # `next((line for line in plan.transition_refusals if label in line), "")`, and every
            # refusal names **both** Shots -- so a Shot that is the incoming side of one refused
            # boundary and the outgoing side of another matched both, and the first one won.
            # Executed with `[a(0,4), b(0,10), c(9,3), d(9.2,2.7)]` and dissolves on `a` and `b`:
            # asking about `b`'s boundary answered with the sentence about `a` nested inside `b`.
            # `assembly.AssemblyPlan.refusal_for` is now the one way to ask, and it takes the two
            # shot ids rather than a label to look for.
            detail = plan.refusal_for(shot.id, after.id)
            raise HTTPException(
                status_code=422,
                detail=(
                    BOUNDARY_PREVIEW_REFUSED_BY_PLAN.format(
                        before=label, after=after_label, detail=detail
                    )
                    if detail
                    else BOUNDARY_PREVIEW_TAKE_MISSING_REFUSAL.format(
                        before=label, after=after_label
                    )
                ),
            )
        segment = plan.clips[index]
        blend_frames = plan.frames[index]
        # The lead is clamped twice, and **the second clamp is provably redundant today**. The
        # leg's trim starts at `round(offset * fps) - lead`, so a lead deeper than the take offset
        # would ask for a negative start frame -- and `trim_args` discards a negative trim in
        # silence, which is the defect measured on this route's sibling in Epic 9: three
        # fingerprints over one byte-identical file, every one of them starting at frame 0.
        #
        # **Recorded as redundant rather than left implying it is load-bearing, because a mutation
        # survived it.** `segment.before.offset` is the Shot's own offset plus the seconds from
        # its start to the Overlap's, and `_margin_frames` is bounded by
        # `clip_frames_on_grid(shot.start, overlap_start)` -- the same seconds on the same grid.
        # So the first is never smaller than the second unless the Shot's own offset is
        # **negative**, and `assembly.take_cut_refusal` has already refused every clip whose cut
        # begins before its take. There is no state that reaches this `min`, and no test can kill
        # it. It is kept for `_final_clip_index`'s reason -- it costs nothing, it says what the
        # number *means*, and it is the line that stays correct if `_margin_frames` ever stops
        # being measured against the Shot's own start.
        lead_frames = min(
            _margin_frames(plan, index - 1, shot.id),
            round(segment.before.offset * ASSEMBLY_FPS),
        )
        tail_frames = _margin_frames(plan, index + 1, after.id)
        width, height = preview_side(plan.width), preview_side(plan.height)
        legs = (
            ("A", segment.before, lead_frames, 0),
            ("B", segment.after, 0, tail_frames),
        )
        by_id = {item.id: item for item in project.shots}
        stacks = [
            [spec.model_dump() for spec in by_id[clip.shot_id].effects]
            for _leg, clip, _lead, _tail in legs
        ]
        looks = discovered_looks() if any(stacks) else ()
        # The envelope, for a boundary either of whose Shots carries a Parameter Binding and for
        # no other -- `render_shot_preview`'s gate, asked through the same helper so the two
        # cannot come to different answers about one measurement.
        driven = any(stack_is_driven(stack) for stack in stacks)
        envelope = (
            preview_envelope(project_id, project, label=segment.label)
            if driven
            else None
        )
        chains: list[EffectStages] = []
        for (leg, clip, lead, tail), stack in zip(legs, stacks, strict=True):
            try:
                chains.append(
                    build_effect_stages(
                        stack,
                        width=width,
                        height=height,
                        luts=looks,
                        # The **export's** width, so the five pixel-denominated parameters know
                        # this grid is half the one their numbers were written for. Identical to
                        # the Shot preview's argument and for the identical reason.
                        reference_width=plan.width,
                        # Where this leg's first frame sits inside its own Shot. The blend's own
                        # leg is `clip.start - clip.approved_start`, which is what the export
                        # passes; the lead moves the outgoing leg's first frame that many frames
                        # earlier, so a time-dependent stage carries on across the seam instead of
                        # restarting inside the blend.
                        clip_offset=clip.start
                        - clip.approved_start
                        - lead / ASSEMBLY_FPS,
                        shot_seconds=clip.approved_duration,
                        envelope=envelope,
                        shot_start=clip.approved_start,
                        # The frames ffmpeg will actually write for this leg, so a compiled
                        # `sendcmd` cannot carry a command past the last one.
                        clip_seconds=(lead + blend_frames + tail) / ASSEMBLY_FPS,
                        leg=leg,
                    )
                )
            except EffectRefusal as refusal:
                raise HTTPException(
                    status_code=422,
                    detail=ASSEMBLY_EFFECTS_REFUSAL.format(
                        shot=clip.label, detail=refusal
                    ),
                ) from refusal
        fingerprint = boundary_fingerprint(
            takes=[segment.before.approved_output, segment.after.approved_output],
            window_start=segment.start,
            lead_frames=lead_frames,
            blend_frames=blend_frames,
            tail_frames=tail_frames,
            offsets=[segment.before.offset, segment.after.offset],
            chains=chains,
            # The **stored** binding spec of every card of both legs, in leg order and then in
            # stack order, and `()` when nothing is driven -- `preview_fingerprint`'s fifth slot
            # exactly, including why the empty case is `()` rather than a shape full of empties.
            bindings=(
                tuple(
                    tuple(
                        tuple(dict(binding) for binding in spec.get("bindings") or ())
                        for spec in stack
                    )
                    for stack in stacks
                )
                if envelope is not None
                else ()
            ),
            song_fingerprint=(
                project.song.analysis.song_fingerprint
                if driven and project.song and project.song.analysis
                else ""
            ),
            transition=entry.transition_id,
            xfade=entry.xfade,
            width=width,
            height=height,
        )
        previews_root = (store.media_dir(project_id) / "previews").resolve()
        relative = f"previews/{fingerprint}.mp4"
        frames = lead_frames + blend_frames + tail_frames
        rendered = await preview_into_cache(
            project_id,
            label=segment.label,
            fingerprint=fingerprint,
            previews_root=previews_root,
            scripts=tuple(
                script for chain in chains for script in chain.scripts
            ),
            argv=lambda scratch: transition_segment_args(
                segment.before.source,
                segment.after.source,
                scratch,
                blend_frames,
                width,
                height,
                entry.xfade,
                before_offset=segment.before.offset,
                after_offset=segment.after.offset,
                preset=PREVIEW_PRESET,
                before_geometry=chains[0].geometry,
                before_treatment=chains[0].treatment,
                after_geometry=chains[1].geometry,
                after_treatment=chains[1].treatment,
                lead_frames=lead_frames,
                tail_frames=tail_frames,
            ),
        )
        return BoundaryPreviewResponse(
            shot_id=shot.id,
            after_shot_id=after.id,
            fingerprint=fingerprint,
            preview=relative,
            preview_url=f"/api/projects/{project_id}/media/{relative}",
            width=width,
            height=height,
            frames=frames,
            window_seconds=frames / ASSEMBLY_FPS,
            lead_frames=lead_frames,
            blend_frames=blend_frames,
            tail_frames=tail_frames,
            transition=entry.transition_id,
            # The blend's own length on the assembly grid, which is the Overlap's length
            # quantised the one way the export quantises it (`clip_frames_on_grid`). **Not a
            # second number**: the transition row's readout and this come out of one Overlap, and
            # a client that drags the Overlap longer moves both.
            transition_seconds=blend_frames / ASSEMBLY_FPS,
            rendered=rendered,
        )

    @app.put(
        "/api/projects/{project_id}/shots/{shot_id}/effects/{index}/bindings",
        response_model=Project,
    )
    def replace_shot_bindings(
        project_id: str, shot_id: str, index: int, request: ShotBindingsRequest
    ) -> Project:
        """Write one effect card's Parameter Bindings — **the only route that mints one**.

        AD-16 and story 10.1's acceptance criterion, as a route rather than as a promise. Every
        other path a stack can arrive by — `PUT .../effects` beside this one, `PUT .../shots` and
        `PUT /api/projects/{id}` through `_adopt_shot_effects`, `POST .../effects/copy` — takes a
        card's bindings off the stored card rather than out of the body (`adopted_effect_stack`),
        which is what makes that a property instead of a convention. This is where the other side
        of it is: a Director says which card, which parameter, which band, which drive and how
        far, and nothing else in this application can say any of it.

        **Addressed by position plus the card's own effect id**, which is what it has been since
        the route existed and is deliberately left alone by R-33. A card now carries an `id` and
        this route could be keyed on it instead — but the client sends the position it *drew* and
        the effect it drew there, a stack edited since refuses by name
        (`SHOT_BINDINGS_CARD_MOVED_REFUSAL`) rather than binding something else's parameter, and
        moving the address would change the wire, the panel and this route for no defect anybody
        has reproduced. R-33 changed how a binding is **adopted** by the generic doors; it did not
        ask this one to be re-addressed, and doing it unasked would be a second change hiding
        inside the first. The stored binding is still keyed by parameter name alone, on the card —
        the index reaches the card and is not kept.

        The gates are `replace_shot_effects`' gates in `replace_shot_effects`' order, and for its
        reasons: the Shot first, so a request naming nothing is a 404 rather than a lecture about
        locks; the lock next, because it is a decision the Director made and it holds whatever the
        body says, in that route's own sentence and at 422 rather than 409 (the Director's
        2026-08-18 ruling — a lock clears by a deliberate act, never by waiting); a body naming no
        bindings refused rather than read as an empty list; then the card; then the catalogue.

        **Validation is `effects.validate_stack` over the whole stack with this card's bindings
        substituted in**, not over the bindings alone, and that is the point of doing it this way:
        the validator is the one thing between a client's numbers and an ffmpeg filter string
        (AD-27), it already knows every binding's shape and bound, and it is what answers *can the
        music drive this?* — in the catalogue's own words, naming the ffmpeg filter that takes no
        runtime commands, or saying that ffmpeg aborts when both `crop` dimensions move. Those
        sentences are carried whole. A binding on `sharpen.amount` is refused here and not
        discovered at render time, because there is nothing to discover at render time: a
        `sendcmd` to a filter that takes no commands is accepted, ignored and reported nowhere.

        **Nothing is stored until every gate passes**, and `store.save` is the last statement, so
        every refusal above leaves the manifest byte-identical.

        The looks are resolved only for a stack that has something in it, which is
        `replace_shot_effects`' rule and is free here: a card being bound is a card that exists,
        so the stack is never empty by the time the folder could be read — but a stack that names
        no look still costs nothing, because `validate_stack` only consults the listing for a
        `LutParameter`.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        if shot.locked:
            raise HTTPException(
                status_code=422,
                detail=SHOT_EFFECTS_LOCKED_REFUSAL.format(shot=shot_label(project, shot)),
            )
        if request.bindings is None:
            raise HTTPException(
                status_code=422,
                detail=SHOT_BINDINGS_ABSENT_REFUSAL.format(shot=shot_label(project, shot)),
            )
        if not request.effect:
            raise HTTPException(
                status_code=422,
                detail=SHOT_BINDINGS_UNNAMED_CARD_REFUSAL.format(
                    shot=shot_label(project, shot)
                ),
            )
        # `0 <= index`, spelled out rather than left to Python's own indexing: `-1` is a perfectly
        # good list index and a perfectly bad thing for a client to have meant.
        if index < 0 or index >= len(shot.effects):
            raise HTTPException(
                status_code=422,
                detail=SHOT_BINDINGS_NO_SUCH_CARD_REFUSAL.format(
                    shot=shot_label(project, shot), count=len(shot.effects), index=index
                ),
            )
        if shot.effects[index].effect != request.effect:
            raise HTTPException(
                status_code=422,
                detail=SHOT_BINDINGS_CARD_MOVED_REFUSAL.format(
                    index=index,
                    shot=shot_label(project, shot),
                    held=shot.effects[index].effect,
                    named=request.effect,
                ),
            )
        stack = [spec.model_dump() for spec in shot.effects]
        stack[index]["bindings"] = request.bindings
        try:
            validate_stack(stack, luts=discovered_looks() if stack else ())
        except EffectRefusal as refusal:
            raise HTTPException(status_code=422, detail=str(refusal)) from refusal
        # Written onto the stored card rather than through `stored_effect_stack`, because this
        # route changes one field of one entry and re-deriving the whole stack from a dump would
        # make every other card's storage depend on a write that was not about it.
        shot.effects[index].bindings = [dict(binding) for binding in request.bindings]
        return store.save(project)

    @app.get(
        "/api/projects/{project_id}/shots/{shot_id}/drive",
        response_model=ShotDriveResponse,
    )
    def read_shot_drive(project_id: str, shot_id: str) -> ShotDriveResponse:
        """One Shot's compiled Parameter Bindings, as the numbers the Drive readout draws.

        **R-27, as a route.** The readout draws the compiled `sendcmd` values themselves rather
        than a curve derived a second way, so this serves what `effects.drive_samples` produced —
        the same walk `effects.sendcmd_script` writes its lines from, sharing one function rather
        than agreeing by test. The alternative R-27 rejected by name was shipping the raw band
        series and computing the drive in the browser, which is a second *renderer*: the picture
        and the export could disagree while every automated gate in this repository passed.

        **`SERVED_ENVELOPE_KEYS`' standing rule, applied to a route instead of a key.** A
        consumer is necessary and not sufficient. This exists because nothing else can answer the
        question: only the compiler knows what a binding compiles to, the per-frame `bands` array
        is about 98 % of a 469 KB sidecar and stays on disk (AD-20), and everything else the
        readout needs — the Shot's window, the stack, the binding's own numbers — is already on
        the wire in the project read and is deliberately not repeated here.

        **Nothing is written, on any path.** It is a read of a manifest and a sidecar; there is no
        `store.save` here and no state derived from the answer.

        **The measurement is read only for a Shot that would compile something**, which is the
        preview route's own rule and for its reason: the verdict hashes the whole master and
        parses a ~405 KB sidecar, and an unbound Shot — every Shot in every project until one is
        bound — must not pay it merely by being selected.

        **Every absence is an empty list, deliberately** (FX-22's *absent, not empty*). No
        binding, a bound card the Director switched off, a song whose measurement has gone, and a
        stored stack `validate_stack` refuses all answer the same way, because the readout's only
        question is whether there is a compiled drive to draw. *Which* absence it is, and the
        action that fixes it, is the band panel's sentence and is already answered there and on
        `GET /timeline/snap-targets`; a second account of it here would be two sources for one
        question, which is exactly what the merged measurement read exists to prevent.

        A sync `def`, so FastAPI runs it in the threadpool: `song_envelope_report` hashes the
        whole master to decide whether the measurement is still current, and a multi-megabyte read
        has no business on the event loop. `snap-targets` is a sync `def` for the same reason.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        # The clip the drive is compiled over, resolved exactly as the preview resolves it: the
        # frames this Shot's window really lands on, over the grid rate. Never `shot.duration`,
        # which is the manifest's float and is what the export rounds *from* — a readout drawn
        # against it would put its last command a fraction of a frame outside the picture above.
        frames = clip_frames_on_grid(shot.start, shot.start + shot.duration)
        seconds = frames / ASSEMBLY_FPS
        stack = [spec.model_dump() for spec in shot.effects]
        answer = ShotDriveResponse(shot_id=shot.id, seconds=seconds)
        if not stack_is_driven(stack):
            return answer
        report = song_envelope_report(project_id, project)
        envelope = report.get("envelope") if report.get("present") else None
        if not isinstance(envelope, dict):
            return answer
        try:
            readouts = drive_readout(
                stack,
                envelope=envelope,
                luts=discovered_looks(),
                shot_start=shot.start,
                clip_seconds=seconds,
            )
        except EffectRefusal:
            # A stored stack the export refuses — a hand-edited manifest, a look whose `.cube` has
            # gone. The effects panel names that fault where the Director can act on it, and the
            # preview refuses it in the export's own words. A readout has nothing to add and
            # nothing to draw, and turning a read into a 422 would put a toast on the screen for
            # merely selecting the Shot.
            return answer
        return ShotDriveResponse(
            shot_id=shot.id,
            seconds=seconds,
            bindings=[
                ShotDriveBinding(
                    index=readout.index,
                    effect=readout.effect_id,
                    parameter=readout.parameter,
                    rest=readout.rest,
                    reach=readout.reach,
                    at=[sample.at for sample in readout.samples],
                    values=[sample.value for sample in readout.samples],
                    silenced=[sample.silenced for sample in readout.samples],
                )
                for readout in readouts
            ],
        )

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/effects/copy",
        response_model=ShotEffectsCopyResponse,
    )
    def copy_shot_effects(
        project_id: str, shot_id: str, request: ShotEffectsCopyRequest
    ) -> ShotEffectsCopyResponse:
        """Copy one Shot's stack onto Shots the caller names, and report what that did.

        **A route rather than a loop of `PUT .../effects`.** A client loop cannot report
        atomically: the fourth write refusing for a lock leaves three shots graded, one not, and
        no single answer about which — and the client has to invent the report itself from four
        replies, which is a decision about what happened living in the caller. One request
        validates once, applies to every unlocked target, and returns one answer.

        **A copy replaces; it never merges.** The target's whole stack is dropped and this Shot's
        is written in its place, which is why the panel states it before the button is reachable
        (`EFFECT_COPY_REPLACES`, and `EFFECT_COPY_CLEARS` for the case below). Merging is
        unstateable anyway: two stacks that both carry a Grain card would have to agree about
        whose strength survives, and no answer to that is one a Director would predict.

        **A source carrying nothing clears its targets, and that is a real write.** It is how a
        Director takes a look back off several shots at once, and it is the write whose name says
        least about what it does — hence the announcement, and hence `effects: 0` in the reply
        rather than an empty report that reads like nothing happened.

        The gates, in the order they have to be in:

        - The source is found first, so a request naming nothing is a 404 rather than a lecture.
        - A body naming no targets is refused by name, and so is an explicitly empty list. Nothing
          in this application applies to "all shots" without the Director choosing them.
        - A named id this project does not hold refuses the **whole** copy. The alternative — apply
          what resolves, mention the rest — is the half-applied write this route exists against.
        - The source among its own targets is refused, because it is a miscounted target set and
          the same miscount silently drops real targets.
        - The source's own stack is **counted** against `SHOT_EFFECT_STACK_LIMIT` and then
          validated, **once**, before a byte reaches any target — so a manifest hand-edited into
          something uncomposable, or into something no command line can hold, cannot be
          multiplied across the plan. The count comes first for the reason the other two doors
          put it first: `validate_stack` asks whether each card composes and never how many there
          are, so a thousand valid cards are a thousand valid answers.

        Only then is anything written, and `store.save` is the last statement — so every refusal
        above leaves the manifest untouched, which is what "nothing was written" has to mean at a
        route as well as in the composer.

        **A locked target is named in the report and the rest still land.** It is *not* a 422 for
        the request, because the request is not unprocessable — it named ten shots and nine of them
        were written, and a status code cannot say that. FX-6's matrix asks for exactly this:
        "Locked targets named, others still applied." The sentence is C1's own
        `SHOT_EFFECTS_LOCKED_REFUSAL`, carried whole, so a lock refuses in one wording wherever it
        refuses — and the Director's 2026-08-18 ruling that a lock is a 422 and never a 409 is
        untouched: no status here is 409, and the whole-request refusals above are all 422.

        **A locked *source* is copied from.** A lock is a fact about that Shot's own stack, and
        this reads it: nothing about the source changes. Refusing here would take away the one
        gesture a finished, locked, graded Shot is most wanted for, and C2's lock note — "its
        effect stack cannot be changed" — would be untrue as a reason for it.
        """
        project = get_project(project_id)
        source = next((item for item in project.shots if item.id == shot_id), None)
        if not source:
            raise HTTPException(status_code=404, detail="Shot not found")
        named = list(request.targets or [])
        if not named:
            raise HTTPException(
                status_code=422,
                detail=SHOT_EFFECTS_COPY_WITHOUT_TARGETS_REFUSAL.format(
                    shot=shot_label(project, source)
                ),
            )
        # Deduplicated in the order named. The same id twice means the same shot once, and a
        # report counting it twice would be reporting a write that did not happen.
        wanted: list[str] = []
        for target_id in named:
            if target_id not in wanted:
                wanted.append(target_id)
        held = {item.id: item for item in project.shots}
        missing = [target_id for target_id in wanted if target_id not in held]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=SHOT_EFFECTS_COPY_UNKNOWN_TARGET_REFUSAL.format(
                    missing=", ".join(missing)
                ),
            )
        if source.id in wanted:
            raise HTTPException(
                status_code=422,
                detail=SHOT_EFFECTS_COPY_ONTO_ITSELF_REFUSAL.format(
                    shot=shot_label(project, source)
                ),
            )
        stack = [spec.model_dump() for spec in source.effects]
        # Capped before it is validated, which is `replace_shot_effects`' order and
        # `_adopt_shot_effects`' order, for `validate_stack`'s reason: it answers "is every card
        # composable" one card at a time, and 985 composable cards are 985 valid answers. This
        # route was the **third** door and the only uncapped one, and it is the worst place for
        # the hole to be: measured 2026-08-26, a hand-edited 985-card Shot copied 200 and planted
        # 985 cards on a clean Shot, N Shots at a time, where the identical stack through
        # `PUT .../effects` answered 422. The source stack was validated and never counted.
        #
        # The source's own count, not the target's: a copy replaces, so what lands on every
        # target is exactly this list and there is nothing else to add it to.
        if len(stack) > SHOT_EFFECT_STACK_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=SHOT_EFFECTS_TOO_MANY_REFUSAL.format(
                    limit=SHOT_EFFECT_STACK_LIMIT, count=len(stack)
                ),
            )
        try:
            # Once, for the whole copy. The looks are resolved only for a stack that names one,
            # which is `replace_shot_effects`' rule: an empty stack cannot name a LUT, and
            # clearing several shots at once must not cost a folder read.
            validate_stack(stack, luts=discovered_looks() if stack else ())
        except EffectRefusal as refusal:
            raise HTTPException(
                status_code=422,
                detail=SHOT_EFFECTS_COPY_UNCOMPOSABLE_REFUSAL.format(
                    shot=shot_label(project, source), detail=str(refusal)
                ),
            ) from refusal
        applied: list[str] = []
        refused: list[ShotEffectsCopyRefusal] = []
        for target_id in wanted:
            target = held[target_id]
            label = shot_label(project, target)
            if target.locked:
                refused.append(
                    ShotEffectsCopyRefusal(
                        shot_id=target.id,
                        shot=label,
                        detail=SHOT_EFFECTS_LOCKED_REFUSAL.format(shot=label),
                    )
                )
                continue
            # Copied deep, never aliased: two Shots sharing one parameter mapping would have one
            # slider move both, and the manifest would not show why. `_adopt_shot_effects` copies
            # for the same reason on the same field.
            #
            # **And minted a new card id per target** (R-33), which is the third of the three
            # doors that clone a card — Split and Duplicate are the other two and mint at
            # `adopted_effect_stack`. Without this, ten targets would hold the source's card ids,
            # a later lookup keyed on a card id would depend on which Shot was read first, and
            # the divergence arrives on the very next slider drag. The bindings ride along
            # unchanged, which is FX-6's whole point and AD-26's: the band average is the same in
            # every Shot's panel, so the band chosen against the reference is the band that lands.
            target.effects = [
                spec.model_copy(deep=True, update={"id": new_id("fx")})
                for spec in source.effects
            ]
            applied.append(label)
        # Saved only when something was written. A copy every one of whose targets was locked
        # changed nothing, and a manifest save for it would touch `updated_at` — which every
        # optimistic-concurrency check in this application reads — for a write that did not happen.
        saved = store.save(project) if applied else project
        return ShotEffectsCopyResponse(
            project=saved,
            source=shot_label(saved, source),
            effects=len(source.effects),
            applied=applied,
            refused=refused,
        )

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/select-take", response_model=Project
    )
    def select_shot_take(
        project_id: str, shot_id: str, request: SelectTakeRequest
    ) -> Project:
        """Point one Shot's `latest_output` at a different clip — an earlier take, or a
        video asset.

        The Director's asks, verbatim (2026-08-20): "Could also use a way to switch the
        selected clip in a shot to a different one if i want" and "I currently have no way
        of attaching a video of my selection from files/assets to a shot i add to the
        timeline". One route for both, because both are the same write: the single
        `latest_output` pointer moves, nothing else. The take strip in the inspector is
        derived client-side from the shot's own job history, so this route only has to
        accept what it can verify:

        - ``output``: a file one of this Shot's own h3 jobs actually produced — the job
          record is the provenance check, so the route cannot be pointed at another
          shot's take by path games.
        - ``asset_id``: a video asset. A generated one already lives under ComfyUI's
          output root and is pointed at directly; an *uploaded* one is copied under
          ``music-video-producer/{project}/clips/`` first, because every reader of
          `latest_output` — the Monitor stream, assembly's probes — resolves against the
          output root and teaching them all a second root is how path handling forks.

        Selecting an external clip clears the over-render bookkeeping (`latest_take_lead`,
        `trim_nudge`): those numbers describe a take rendered with the margin, and carried
        onto a hand-picked clip they would cut its opening quarter-second for no reason.
        A draft shot gains `complete` — it has a clip now, which is what the status tracks.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        if shot.locked:
            raise HTTPException(
                status_code=422,
                detail=SELECT_TAKE_LOCKED.format(shot=shot_label(project, shot)),
            )
        output_root = (settings.comfy_root / "output").resolve()
        if request.output:
            produced = {
                file
                for job in project.jobs
                if job.kind == "h3" and job.target_id == shot.id
                for file in job.output_files
            }
            if request.output not in produced:
                raise HTTPException(
                    status_code=422,
                    detail=SELECT_TAKE_UNKNOWN.format(shot=shot_label(project, shot)),
                )
            target = (output_root / Path(request.output)).resolve()
            if output_root not in target.parents or not target.is_file():
                raise HTTPException(
                    status_code=404,
                    detail=TAKE_MISSING_FILE_REFUSAL.format(
                        shot=shot_label(project, shot), path=request.output
                    ),
                )
            if shot.latest_output != request.output:
                shot.latest_review = None
            shot.latest_output = request.output
        elif request.asset_id:
            asset = next((a for a in project.assets if a.id == request.asset_id), None)
            if asset is None:
                raise HTTPException(status_code=404, detail="Asset not found")
            if asset.kind != "video":
                raise HTTPException(
                    status_code=422, detail=SELECT_TAKE_NOT_VIDEO.format(name=asset.name)
                )
            source = resolve_asset_path(project_id, asset)
            if asset.source == "upload":
                clips_dir = output_root / "music-video-producer" / project_id / "clips"
                clips_dir.mkdir(parents=True, exist_ok=True)
                landed = clips_dir / f"{asset.id}{source.suffix}"
                if not landed.is_file():
                    shutil.copyfile(source, landed)
                pointer = landed.relative_to(output_root).as_posix()
            else:
                pointer = source.relative_to(output_root).as_posix()
            if shot.latest_output != pointer:
                shot.latest_review = None
            shot.latest_output = pointer
            # External clip: no over-render margin exists in it, so no lead to cut — and no
            # window snapshot either, because nothing rendered this file for a window of this
            # plan. Cleared together with the lead so the pair cannot claim a provenance the
            # clip does not have; `restore_song_audio` reads the absence and says so.
            shot.latest_take_lead = 0.0
            shot.latest_take_start = 0.0
            shot.latest_take_duration = 0.0
            shot.trim_nudge = 0.0
        else:
            raise HTTPException(status_code=422, detail=SELECT_TAKE_EMPTY)
        if shot.status in ("draft", "ready"):
            shot.status = "complete"
        return store.save(project)

    @app.get("/api/projects/{project_id}/shots/{shot_id}/take")
    def read_shot_take(project_id: str, shot_id: str) -> FileResponse:
        """Stream one Shot's latest take to the browser, by ids and by nothing else.

        The URL carries a project id and a shot id and **no path**. The file served is resolved
        here from the Shot's own `latest_output` through `analyze_latest_take`'s resolution,
        containment check included, so there is no path-injection surface to defend: a client
        cannot ask this route for anything except what the manifest says the Shot produced. That
        is the same discipline `read_project_media` applies to the media tree, pointed at
        ComfyUI's output root, where takes actually land.

        Starlette's `FileResponse` answers `Range` itself — verified on this installation, 1.6.0:
        a `bytes=` request gets a 206 with the right `Content-Range`, a suffix or open-ended
        range is served, an unsatisfiable one gets a 416, and the plain 200 advertises
        `Accept-Ranges: bytes`. That is what makes the `<video>` element's scrub bar work, and a
        route test holds it to a real 206 so a change of response class cannot silently turn
        seeking off.

        Both failure rows are 404s with a sentence, per the matrix: the code is for the `<video>`
        element, which treats every error alike, and the sentence is for the Director. The
        missing-file row names the path this looked for, because a manifest pointing at a file
        that is gone is usually a moved or cleared ComfyUI output directory and the path is the
        only way to tell which.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        if not shot.latest_output:
            raise HTTPException(
                status_code=404,
                detail=TAKE_NOT_RENDERED_REFUSAL.format(shot=shot_label(project, shot)),
            )
        output_root = (settings.comfy_root / "output").resolve()
        target = (output_root / Path(shot.latest_output)).resolve()
        if output_root not in target.parents or not target.is_file():
            raise HTTPException(
                status_code=404,
                detail=TAKE_MISSING_FILE_REFUSAL.format(
                    shot=shot_label(project, shot), path=shot.latest_output
                ),
            )
        return FileResponse(target)

    @app.post("/api/projects/{project_id}/shots/expand-prompts", response_model=Project)
    async def expand_plan_prompts(project_id: str) -> Project:
        """Expand every shot in the plan into H3's format. Pass two, over the whole plan.

        **N sequential model calls, one per shot** — `expand_shots` is what makes that true and
        says why. This route is the plan-wide half of `expand_shot_prompt` above, and the two share
        every rule: the same refusals in the same order, the same format check before any write,
        and the same field.

        No body. Every shot is judged, including the ones nothing can be written to, because "why
        did nothing happen to that shot" is the question a sweep has to answer — a locked shot the
        sweep silently skipped is indistinguishable to the Director from one it forgot.

        **Nothing is persisted until every shot has been judged.** There is one terminal
        `store.save`, and `apply_expansions` commits in one pass after the loop, so a failure
        part-way through leaves the manifest and the in-memory project untouched rather than
        half-applied. Phase one's own mutation testing established that the terminal save is what
        makes that structural, rather than the staging in front of it.

        The project is re-read after the sweep for `director_chat`'s reason, and here it matters
        more than anywhere else in this module: the sweep is many model calls long, so a shot can
        be locked, rendered or deleted several times over while it runs.
        """
        # The one snapshot every payload is built from, so every call sees a consistent plan.
        snapshot = get_project(project_id)
        if not snapshot.shots:
            raise HTTPException(status_code=422, detail=EXPAND_PROMPTS_WITHOUT_SHOTS)
        # Song order, which is the order the Director watches the video in and the order the
        # neighbours' intents make sense in — "rinse and repeat for the next", in their own words.
        swept = ordered_shots(snapshot)
        try:
            outcomes = await expand_shots(snapshot, swept, director=director)
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

        project = get_project(project_id)
        # Re-checked rather than assumed from the snapshot, exactly as expansion re-checks: saving
        # a reply about a plan that no longer has any shots would leave the thread asserting a
        # sweep of nothing.
        if not project.shots:
            raise HTTPException(status_code=422, detail=EXPAND_PROMPTS_WITHOUT_SHOTS)
        committed = apply_expansions(project, outcomes)
        labels = {shot.id: shot_label(project, shot) for shot in project.shots}
        project.messages.append(
            assistant_reply(
                EXPAND_PROMPTS_MESSAGE, expansion_sweep_notices(committed, labels)
            )
        )
        return store.save(project)

    @app.post("/api/projects/{project_id}/shots/{shot_id}/approve", response_model=Project)
    def approve_take(project_id: str, shot_id: str) -> Project:
        """Approve one Shot's latest take. FR-21: explicit, reversible, never automatic. No body.

        **This is the one writer of approval.** Nothing else in this application assigns
        `approved_output` or the `approved` status — not job completion (`apply_job_history`
        deliberately stops at `complete`), not the assistant, not expansion — and a test scans
        the whole package to keep it that way. What is written is what the server resolved from
        its own manifest: `approved_output := latest_output`, never a value from the wire.
        `approved_output` is about to become assembly's input, and a path the server copied from
        its own record of what rendered is evidence; a path accepted from a client would be a
        claim. This route binds no body at all, so there is nothing on the wire to trust.

        Both fields move together, and the pairing is what makes the un-approve path honest:
        while the approval stands, render-again and mark-ready refuse this Shot, so
        `latest_output` cannot move and `approved_output == latest_output` holds for the life of
        the approval. A test pins that invariant end to end rather than trusting it.

        The refusal order is the house order. In flight first, from the job records as well as
        the status — `shot_render_in_flight` — because a status walked back by hand through the
        generic shots write is exactly what hides a live render, and approving a take that is
        about to be displaced attaches the decision to whichever file lands next; 409, because a
        live render is a state conflict the same request survives. Then idempotence: an approved
        Shot answers 200 and nothing is rewritten, not even `updated_at`. Then the take gate:
        approval is a decision about a specific piece of media, so a Shot that never produced
        one has nothing to approve, and that is a 422 fact no waiting changes.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        if shot_render_in_flight(project, shot):
            raise HTTPException(
                status_code=409,
                detail=APPROVE_IN_FLIGHT_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # Idempotent, and genuinely a no-op: nothing is saved, so an unchanged manifest does not
        # get a fresh `updated_at` to collide with the next optimistic-concurrency check.
        if shot.approved_output:
            return project
        if not shot.latest_output:
            raise HTTPException(
                status_code=422,
                detail=APPROVE_NO_TAKE_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # The whole write, every half together. The value is the server's own resolution of
        # what this Shot's take is; nothing from the request is on the right-hand side. The
        # window snapshot (AD-13) rides in the same write: the approval is a decision about
        # this take *in this window*, and assembly refuses the Shot if the window moves
        # afterward — see `Shot.approved_start`.
        shot.approved_output = shot.latest_output
        shot.status = "approved"
        shot.approved_start = shot.start
        shot.approved_duration = shot.duration
        return store.save(project)

    @app.post("/api/projects/{project_id}/shots/{shot_id}/unapprove", response_model=Project)
    def unapprove_take(project_id: str, shot_id: str) -> Project:
        """Clear one Shot's approval. The reversal FR-21 promises, and the one way back. No body.

        Un-approval is what re-enables everything that keys on approval — render-again,
        mark-ready, expansion and the assistant all refuse an approved Shot, and none of them
        may be weakened instead — so this route accepts *either* approval signal through
        `shot_is_approved`, the same definition render-again refuses by. A Shot with the
        `approved` status and no `approved_output`, reachable only by hand through the generic
        shots write, would otherwise be a Shot nothing can move: mark-ready disowns the status,
        render-again says to clear the approval, and a route that only recognised
        `approved_output` would refuse to.

        Both fields are cleared together, `status` back to `complete` per the matrix — the Shot
        had a take when it was approved, and a complete Shot is exactly what it goes back to
        being, re-renderable through render-again like any other. Nothing else is touched:
        `latest_output` stays, the take stays on disk, and the refusal for a Shot that is not
        approved names what the Shot actually is rather than only refusing.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        if not shot_is_approved(shot):
            raise HTTPException(
                status_code=422,
                detail=UNAPPROVE_NOT_APPROVED_REFUSAL.format(
                    shot=shot_label(project, shot), status=shot.status
                ),
            )
        # The whole write: the decision is withdrawn, the record of what rendered is not.
        # The window snapshot goes with it — it described the withdrawn approval, and a
        # snapshot outliving its approval would make the *next* approval's staleness check
        # read a window nobody decided about.
        shot.approved_output = ""
        shot.status = "complete"
        shot.approved_start = 0
        shot.approved_duration = 0
        return store.save(project)

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/expand-prompt",
        response_model=ShotExpansionResult,
    )
    async def expand_shot_prompt(project_id: str, shot_id: str) -> ShotExpansionResult:
        """Turn one Shot's intent into an H3-format prompt. Pass two, one Shot at a time.

        No body: everything this needs is already on the Shot and its project. The whole-plan
        `director/expand` above is pass one and is untouched — it lays shots out so they flow
        together, in one call, because cross-shot variance is a property of the plan. This is
        the opposite shape for the opposite reason: one H3 prompt is long, and thirty of them
        will not fit a single context.

        Refusal order matters and is the same one every other automated writer uses: whether
        this Shot may be written to at all comes before whether there is anything to write
        from. A locked Shot with an empty intent should hear that it is locked — telling it to
        write an intent first would send the Director to do work that would then be refused.

        **A malformed answer is not stored.** The checker runs before the write, and a prompt
        that fails it is retried — `attempt_expansion` owns the loop and its budget, shared
        with the sweep so the two paths cannot drift — and only when every attempt fails is
        the last one returned with its problems rather than saved. Storing it would put a
        broken prompt in the manifest that the *next render* would submit, which is exactly the
        outcome checking before a render exists to prevent — and the failure would surface as a
        bad take rather than as a message.
        """
        project = get_project(project_id)
        shot = next((held for held in project.shots if held.id == shot_id), None)
        if shot is None:
            raise HTTPException(status_code=404, detail="Shot not found")

        label = shot_label(project, shot)
        if reason := expansion_write_refusal(shot):
            wording = (
                EXPAND_PROMPT_LOCKED if reason == "locked" else EXPAND_PROMPT_RENDERED
            )
            raise HTTPException(status_code=422, detail=wording.format(shot=label))
        if prompt_is_missing(shot):
            raise HTTPException(
                status_code=422, detail=EXPAND_PROMPT_WITHOUT_INTENT.format(shot=label)
            )

        mode = resolve_shot_mode(shot)
        try:
            outcome = await attempt_expansion(project, shot, director=director)
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if outcome.kind == "failed":
            raise HTTPException(status_code=502, detail=outcome.detail)
        if outcome.kind == "malformed":
            return ShotExpansionResult(
                project=project,
                applied=False,
                problems=list(outcome.problems),
                prompt=outcome.text,
                note=EXPAND_PROMPT_MALFORMED,
                attempts=outcome.attempts,
            )

        # Re-checked pure so the advisory problems ride along with an applied answer, exactly
        # as they always have: `attempt_expansion` only reports problems for a refusal.
        # A song-audio reference shot's outcome is deterministic prose, not a document —
        # the H3 checker would only report the fields it deliberately does not have.
        advisory: list[str] = []
        if not (shot.use_song_audio and mode == "references"):
            advisory = [
                problem.message
                for problem in h3_check(
                    outcome.text,
                    duration=shot.duration,
                    expect_instruction=mode in H3_KEYFRAME_MODES,
                    forbid_dialogue=shot.use_song_audio,
                    # The under-citation half of the reference bounds surfaces here and only
                    # here: it is advisory, so it rides along with an applied answer rather
                    # than refusing one.
                    reference_slots=reference_slot_counts(project, shot),
                ).problems
            ]

        # Re-read after the await for the reason `director_chat` documents: the Shot may have
        # been locked, rendered or deleted while the model was thinking, and the answer was
        # written against a snapshot that no longer describes it.
        project = get_project(project_id)
        current = next((held for held in project.shots if held.id == shot_id), None)
        if current is None:
            raise HTTPException(status_code=404, detail="Shot not found")
        if reason := expansion_write_refusal(current):
            wording = (
                EXPAND_PROMPT_LOCKED if reason == "locked" else EXPAND_PROMPT_RENDERED
            )
            raise HTTPException(
                status_code=422,
                detail=wording.format(shot=shot_label(project, current)),
            )

        # The same song-audio field normalization the sweep applies; see
        # `normalize_audio_fields`.
        current.h3_prompt = (
            normalize_audio_fields(outcome.text, audio_tag=song_audio_tag(project, current))
            if current.use_song_audio
            else outcome.text
        )
        # Beside it, `apply_expansions`' line and for its reason: the map this prompt was written
        # against, so a citation moved afterwards is decidable rather than invisible. Taken off the
        # re-read project, which is the one this write lands on.
        current.h3_prompt_map = reference_map_sentence(
            reference_map_tag_lines(project, current)
        )
        store.save(project)
        return ShotExpansionResult(
            project=project,
            applied=True,
            problems=advisory,
            prompt=outcome.text,
            attempts=outcome.attempts,
        )
