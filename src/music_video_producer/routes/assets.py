"""Assets: the library's images and the files this application serves back.

`upload`, `fill`, `multiview`, `edit` and `name` are here too. They used to be held in `app.py`
by the enumeration in `tests/test_api.py` that counts `Asset(` constructions and
`asset.name = ` assignments -- five and two -- because it counted them in that one file. It
counts them across `src/music_video_producer/` now, so a sixth construction or a third rename
door fails it wherever it is added, including here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from ..app import (
    ASSET_FILL_CONFIRM_REFUSAL,
    ASSET_FILL_NO_PROPOSALS_REFUSAL,
    ASSET_FILL_RENDERS_OPEN_REFUSAL,
    ASSET_NAME_EMPTY,
    ASSET_NAME_LIMIT,
    ASSET_NAME_TOO_LONG,
    ASSET_RENAME_APPLIED,
    ASSET_RENAME_CHILDREN,
    ASSET_RENAME_MAPS,
    ASSET_RENAME_OVER_MATCHES,
    ASSET_RENAME_PROMPTS,
    ASSET_RENAME_UNSCANNABLE,
    CHARACTER_SLOT_NOT_A_CHARACTER,
    CHARACTER_SLOT_TAKEN,
    CONSISTENCY_PROMPT_LIMIT,
    CONSISTENCY_PROMPT_TOO_LONG,
    DELETE_ASSET_CITED,
    DIRECTOR_CONTEXT_EXCLUDE,
    EXPANSION_LOCKED_NOTICE,
    MULTIVIEW_SUBJECTS,
    REPLACE_ASSET_APPROVED_NOTE,
    REPLACE_ASSET_FREED,
    REPLACE_ASSET_IN_FLIGHT,
    REPLACE_ASSET_KIND_CHANGE,
    REPLACE_ASSET_RENDERED_NOTE,
    REPLACE_ASSET_REPORT,
    REPLACE_ASSET_STILL_CITED,
    REPLACE_ASSET_UNCITED,
    REPLACE_ASSET_UNKNOWN,
    REPLACE_ASSET_WITH_ITSELF,
    AssetCharacterSlotRequest,
    AssetConsistencyRequest,
    AssetEditRequest,
    AssetFillRequest,
    AssetFillResponse,
    AssetFillSubmission,
    AssetNameRequest,
    AssetRenameResponse,
    AssetReplacementRequest,
    AssetReplacementResponse,
    AssetReplacementSkip,
    MultiviewRequest,
    _copy_upload,
    _replacement_row,
    _safe_filename,
    _vision_media,
    multiview_refusal,
    refresh_reference_maps,
    shot_render_in_flight,
    shot_render_provenance,
)
from ..asset_replacement import asset_replacement_plan
from ..batch import PENDING_SUBMISSION_PROMPT_ID, accept_submission, reconcilable_jobs, shot_label
from ..comfy import ComfyError
from ..director import DirectorError, DirectorUnavailable
from ..models import (
    NAME_SCAN_MIN_LENGTH,
    Asset,
    Project,
    RenderJob,
    VisionInspectionRecord,
    character_slot_assets,
)
from ..timeline import ordered_shots
from ..workflows import (
    H3_REFERENCE_LIMITS,
    build_flux_payload,
    build_h3_image_edit_payload,
    build_multiview_payload,
    image_edit_prompt,
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
    get_project = ctx.get_project
    record_submission = ctx.record_submission
    resolve_asset_path = ctx.resolve_asset_path
    settings = ctx.settings
    settle_unsubmitted_jobs = ctx.settle_unsubmitted_jobs
    store = ctx.store

    @app.put(
        "/api/projects/{project_id}/assets/{asset_id}/consistency-prompt",
        response_model=Project,
    )
    def replace_consistency_prompt(
        project_id: str, asset_id: str, request: AssetConsistencyRequest
    ) -> Project:
        """Set this Asset's appearance anchor — the Director's own words, and the only writer.

        The anchor wins over the generation prompt and over the vision summary everywhere a
        description of this asset is consumed (`timeline._asset_description` writes that
        ordering down), so it must never be written by anything that guesses. **Nothing in
        this application infers one**: no route derives it from `Asset.prompt`, the vision
        inspection route writes `vision` and only `vision`, no tool schema exposes it to a
        model, and the generic full-project `PUT` re-adopts the stored value rather than
        trusting a body. This route is the one door.

        Written onto the *stored* Asset rather than a rebuilt one, `replace_song_context`'s
        rule: there is no construction site here where `path`, `source` or `prompt_id` could
        be defaulted away by an edit that was only ever about one string.

        An empty body clears the anchor, which is what emptying the box means. Trimmed at the
        edges and bounded by `CONSISTENCY_PROMPT_LIMIT`, measured after trimming; the refusal
        happens before anything is assigned, so a rejected anchor leaves the asset untouched.
        """
        project = get_project(project_id)
        asset = next((item for item in project.assets if item.id == asset_id), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        anchor = request.consistency_prompt.strip()
        if len(anchor) > CONSISTENCY_PROMPT_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=CONSISTENCY_PROMPT_TOO_LONG.format(
                    name=asset.name, length=len(anchor), limit=CONSISTENCY_PROMPT_LIMIT
                ),
            )
        asset.consistency_prompt = anchor
        # The anchor is *in* the map — `anchored_label` composes it into every tag line — so
        # setting or clearing one changes what every shot citing this asset should be saying about
        # it. Free to re-derive for the prose shots, and recorded as stale for the rest.
        refresh_reference_maps(project)
        return store.save(project)

    @app.put(
        "/api/projects/{project_id}/assets/{asset_id}/character-slot",
        response_model=Project,
    )
    def replace_character_slot(
        project_id: str, asset_id: str, request: AssetCharacterSlotRequest
    ) -> Project:
        """Link this character asset to a singer, by slot number — the one writer of the link.

        The Director's decision (2026-08-21): a lyric line tagged `(S1)` resolves to whichever
        character asset holds slot 1. The number is on the Asset rather than the asset id being in
        the sheet, so replacing a character is one re-slot instead of an edit to every tagged line.

        Three refusals, and each leaves the asset exactly as it was because the check happens
        before anything is assigned:

        * the asset is not a `character`. A slot names a singer, and storing one on a prop would
          make `(S1)` resolve to a thing that cannot sing (`CHARACTER_SLOT_NOT_A_CHARACTER`);
        * another asset already holds the slot. One slot, one character — otherwise a tagged line
          points at two references and the render picks by accident (`CHARACTER_SLOT_TAKEN`);
        * the number is outside `CHARACTER_SLOT_LIMIT`, refused by the schema before the route
          runs, so no asset can hold a slot no dropdown can name.

        `0` clears the slot, which is what "not one of the singers" means, and is a genuine no-op
        for everything downstream: an unslotted library is what every existing project has and
        `character_slot_assets` answers `{}` for it.

        **The one door.** Nothing infers a slot — no route hands the only character asset slot 1
        because it is the only one, no tool schema exposes it to a model, and the generic
        full-project `PUT` re-adopts the stored value per asset id.

        Writes no shot and re-derives no map, which is where it differs from
        `replace_consistency_prompt` deliberately: an anchor is *in* the reference map's tag lines,
        so setting one changes what every citing shot says. A slot is in no prompt anywhere in pass
        1 — populate consuming it is pass 2 — so re-deriving anything here would be spending a
        sweep to produce identical text.
        """
        project = get_project(project_id)
        asset = next((item for item in project.assets if item.id == asset_id), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        if asset.kind != "character":
            raise HTTPException(
                status_code=422,
                detail=CHARACTER_SLOT_NOT_A_CHARACTER.format(
                    name=asset.name, kind=asset.kind
                ),
            )
        slot = request.character_slot
        # Read through `character_slot_assets`, which is the same resolution a `(S1)` mark gets,
        # so the route cannot admit a pair this application would later have to break a tie over.
        # An asset re-asserting its own slot is not a collision.
        holder = character_slot_assets(project).get(slot)
        if slot and holder is not None and holder.id != asset_id:
            raise HTTPException(
                status_code=422,
                detail=CHARACTER_SLOT_TAKEN.format(slot=slot, name=holder.name),
            )
        asset.character_slot = slot
        return store.save(project)

    @app.delete("/api/projects/{project_id}/assets/{asset_id}", response_model=Project)
    def delete_asset(project_id: str, asset_id: str) -> Project:
        """Remove one asset from the library — refused by name while any shot cites it.

        Two dialogs promised this ("keep, delete, or AI Mod"; "delete it to reject") and
        no route existed (the analyst's finding, 2026-08-20). The citation refusal names
        the shots because a dangling citation is the render-time 422 this route would
        otherwise be manufacturing. An uploaded asset's file goes with it; a generated
        asset's file stays in ComfyUI's output tree, same rule as project deletion.
        """
        project = get_project(project_id)
        asset = next((item for item in project.assets if item.id == asset_id), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        citing = [
            shot_label(project, shot)
            for shot in ordered_shots(project)
            if any(citation.asset_id == asset_id for citation in shot.citations)
        ]
        if citing:
            raise HTTPException(
                status_code=422,
                detail=DELETE_ASSET_CITED.format(name=asset.name, shots=", ".join(citing)),
            )
        if asset.source == "upload" and asset.path:
            target = store.project_dir(project_id) / asset.path
            if target.is_file():
                target.unlink()
        project.assets = [item for item in project.assets if item.id != asset_id]
        # A location that is no longer in the library is not this project's location. Cleared
        # here rather than left to `default_setting_asset`'s re-validation — which would also
        # no-op it — so the manifest never carries a pointer to something that is gone, and a
        # later asset re-using the id could not silently inherit the declaration.
        if project.default_setting_id == asset_id:
            project.default_setting_id = ""
        return store.save(project)

    @app.post(
        "/api/projects/{project_id}/assets/{asset_id}/replace-citations",
        response_model=AssetReplacementResponse,
    )
    def replace_asset_citations(
        project_id: str, asset_id: str, request: AssetReplacementRequest
    ) -> AssetReplacementResponse:
        """Re-point every shot citing this asset at another one. The way through `delete_asset`.

        The Director hit `DELETE_ASSET_CITED` trying to remove the HarderFaster source now that
        its Krea multiview exists, liked that it was caught, and asked for the act the refusal
        implies: "a nice Replace With/Cancel option set ... so then i could select another image
        while i am here in assets and auto replace the one i am trying to remove across the
        affected shots" (2026-08-20). **The delete refusal is untouched**, and this route does
        not delete: it moves citations, and the Director deletes afterwards — so an asset one
        locked shot still cites meets the same refusal for the same reason, rather than
        half-vanishing from a library it is still referenced in.

        Report first, apply on confirm, enforced here rather than trusted to the browser —
        `snap_timeline_cuts`' shape, which is `populate`'s `confirm_replace` in a smaller key.
        Without `confirm_apply` this route **does not call `store.save`** and the response
        carries no project at all, so "nothing was written" is visible on the wire.

        Every decision is `asset_replacement.asset_replacement_plan`'s. This route's own additions
        are the two lookups, the three refusals, the protection map, the kind warning, the
        sentences, and the write.

        **A rendered shot is replaced, and told about — `shot_write_refusal` is deliberately not
        the gate here.** This route shipped using it and the Director overruled that the same day:
        *"So even with takes we do want the asset for the shot replaceable, that way a re-render
        would use the updated asset without losing previous takes. This helps facilitate
        experimentation."* The reasoning is the general rule for citations and is worth keeping in
        one place: **replacing a citation does not touch the take.** The file is still on disk,
        `latest_output` still names it, the takes strip still lists it, `RenderJob.output_files` is
        unchanged, and a citation describes what a *future* render would use. `shot_write_refusal`
        is right about prose — an in-place prompt rewrite really does destroy the record — and its
        `rendered` arm does not transfer to a field that is not the prompt. `timeline.
        window_move_refusal` already reasons this way for windows, for the same reason.

        What survives is the report. `REPLACE_ASSET_RENDERED_NOTE` and
        `REPLACE_ASSET_APPROVED_NOTE` name those shots before the confirm, because the consequence
        is real and unrecoverable: nothing in this application records which assets produced a
        take, so afterwards the take and the references beside it simply disagree with no way back.

        **Approved shots are the same case, and their approval is untouched.** No path here writes
        `approved_output`, `approved_start` or `approved_duration`. AD-13's staleness comparison is
        between the stored window and the live one, and citations are not the window, so assembly
        reads exactly what it read before. They get their own report line rather than their own
        rule, because an approval is a stronger statement than a render and the count should be
        visible.

        **Two protections remain.** A `locked` shot is an explicit hands-off the Director set and
        only they may clear it. An **in-flight** render is the one genuine correctness block: the
        job was submitted against the old asset and is executing now, so rewriting the citation
        underneath it would leave that job's record describing a render that never happened. Read
        through `shot_render_in_flight`, the single reader of the job records, which also catches
        a shot whose status was walked backwards by hand.

        Nothing renders, arms, queues or approves. `comfy` is not touched on any path, no
        `status` moves, and the only fields any shot differs in afterwards are `citations`,
        `reference_labels` and the `asset_ids` projection the model rebuilds from the first.
        """
        project = get_project(project_id)
        replaced = next((item for item in project.assets if item.id == asset_id), None)
        if replaced is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        replacement = next(
            (item for item in project.assets if item.id == request.replacement_id), None
        )
        if replacement is None:
            raise HTTPException(status_code=404, detail=REPLACE_ASSET_UNKNOWN)
        # Before the plan, because a self-replacement is not an empty plan: every citation would
        # match, every shot would be reported as changed, and the manifest would be rewritten to
        # exactly what it already said. A report claiming thirty changes and a save that changes
        # nothing is worse than a refusal.
        if replacement.id == replaced.id:
            raise HTTPException(
                status_code=422,
                detail=REPLACE_ASSET_WITH_ITSELF.format(name=replaced.name),
            )
        # Two protections and no more. A lock is the Director's own hands-off and only they clear
        # it; a render executing right now is the one genuine correctness block. `shot_write_refusal`
        # is deliberately NOT the gate here — see the ruling in the docstring — so the lock is read
        # from the Shot directly rather than through a function whose `rendered` arm this route no
        # longer honours. In-flight is `shot_render_in_flight`, the one reader of the job records,
        # rather than a second walk over them.
        protected: dict[str, str] = {}
        for shot in project.shots:
            if shot.locked:
                protected[shot.id] = EXPANSION_LOCKED_NOTICE.format(
                    shots=shot_label(project, shot)
                )
            elif shot_render_in_flight(project, shot):
                protected[shot.id] = REPLACE_ASSET_IN_FLIGHT.format(
                    shot=shot_label(project, shot), replaced=replaced.name
                )
        # Not a protection: a note. Approved outranks rendered because an approval is the stronger
        # statement about the same take, and a shot reported under both headings would be counted
        # twice. `shot_render_provenance` is the same predicate `shot_write_refusal`'s second arm
        # reads — the fact is unchanged, only what this route does about it.
        provenance = {
            shot.id: (
                "approved"
                if shot.approved_output or shot.status == "approved"
                else "rendered"
            )
            for shot in project.shots
            if shot_render_provenance(shot)
        }
        plan = asset_replacement_plan(
            project,
            replaced=replaced,
            replacement=replacement,
            protected=protected,
            provenance=provenance,
            limits=H3_REFERENCE_LIMITS,
        )
        # The honest-empty refusal, `snap_timeline_cuts`' rule: nothing cites this asset, so there
        # is no plan to report over. Checked on `cited` rather than on the writable buckets, so an
        # asset every one of whose citing shots is locked still *reports* — those skips are the
        # answer to "why can I still not delete it", and refusing them into a 422 would hide it.
        if not plan.cited:
            raise HTTPException(
                status_code=422,
                detail=REPLACE_ASSET_UNCITED.format(name=replaced.name),
            )
        still_cited = len(plan.skips)
        # The two provenance lines, each drawn only when it has shots. Grouped rather than one row
        # per shot, `expansion_sweep_notices`' rule: listing twenty shots through a `{shot}`-shaped
        # sentence twenty times is not a report anyone reads.
        rendered = plan.with_provenance("rendered")
        approved = plan.with_provenance("approved")
        notes = [
            wording.format(
                count=len(changes),
                replaced=replaced.name,
                shots=", ".join(change.label for change in changes),
            )
            for wording, changes in (
                (REPLACE_ASSET_APPROVED_NOTE, approved),
                (REPLACE_ASSET_RENDERED_NOTE, rendered),
            )
            if changes
        ]
        response = AssetReplacementResponse(
            applied=False,
            replaced=replaced.name,
            replacement=replacement.name,
            swapped=len(plan.swaps),
            merged=len(plan.merges),
            skipped=len(plan.skips),
            still_cited=still_cited,
            rendered=len(rendered),
            approved=len(approved),
            notes=notes,
            swaps=[_replacement_row(change) for change in plan.swaps],
            merges=[_replacement_row(change) for change in plan.merges],
            skips=[
                AssetReplacementSkip(
                    shot_id=skip.shot_id, label=skip.label, reason=skip.reason
                )
                for skip in plan.skips
            ],
            warning=(
                ""
                if replacement.kind == replaced.kind
                else REPLACE_ASSET_KIND_CHANGE.format(
                    replacement=replacement.name,
                    replacement_kind=replacement.kind,
                    replaced=replaced.name,
                    replaced_kind=replaced.kind,
                )
            ),
            message=" ".join(
                (
                    REPLACE_ASSET_REPORT.format(
                        replacement=replacement.name,
                        replaced=replaced.name,
                        swapped=len(plan.swaps),
                        merged=len(plan.merges),
                        skipped=len(plan.skips),
                    ),
                    REPLACE_ASSET_FREED.format(replaced=replaced.name)
                    if not still_cited
                    else REPLACE_ASSET_STILL_CITED.format(
                        count=still_cited, replaced=replaced.name
                    ),
                )
            ),
        )
        if not request.confirm_apply or not plan.writes:
            return response
        # Committed by position from the plan's own candidates, `assistant_fill`'s one pass after
        # every shot has been judged: nothing above this line touched the project, so a plan that
        # raised part-way through would have left both the manifest and the in-memory project
        # exactly as they were.
        for index, shot in enumerate(project.shots):
            if (candidate := plan.candidates.get(shot.id)) is not None:
                project.shots[index] = candidate
        # Where the free rebuild pays off most: this route rewrites citations across up to 22
        # shots at once, and every one of them whose expansion is the prose form is re-derived in
        # this one pass, with no model call. After the commit loop and before the single save, so
        # a plan that raised part-way through has still written nothing — the guarantee this
        # route's own commit loop makes.
        refresh_reference_maps(project)
        response.project = store.save(project)
        response.applied = True
        return response

    @app.get("/api/projects/{project_id}/media/{media_path:path}")
    def read_project_media(project_id: str, media_path: str) -> FileResponse:
        get_project(project_id)
        media_root = store.media_dir(project_id).resolve()
        target = (media_root / media_path).resolve()
        if media_root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="Media not found")
        return FileResponse(target)

    @app.get("/api/projects/{project_id}/assets/{asset_id}/file")
    def read_asset_file(project_id: str, asset_id: str) -> FileResponse:
        """One asset's bytes, served from disk by this application.

        The Assets grid used to point every generated thumbnail at ComfyUI's `/view`, so the
        whole library went blank whenever ComfyUI was down — which is routine, and down for
        reasons that have nothing to do with browsing a library. The bytes were never
        ComfyUI's to withhold: `resolve_asset_path` already reads them off the same disk this
        process is running on, and `read_shot_take` beside it already serves a take that way.

        **Addressed by id, never by path**, which is the difference between this and `/view`.
        The path is read from the manifest, and `resolve_asset_path` containment-checks the
        result against the one root that asset's `source` allows — so a manifest edited to
        carry `../../` resolves outside the root and 404s rather than serving it.

        An asset with no output yet 404s here rather than being special-cased: the browser
        already decides *not to ask* when `path` is empty (it draws `RENDERING`/`NO PREVIEW`
        instead), and a route that invented a placeholder would be a second, quieter answer to
        a question the grid has already answered.

        Nothing renders, queues or approves, and `comfy` is not touched on any path — that is
        the whole point.
        """
        project = get_project(project_id)
        asset = next((item for item in project.assets if item.id == asset_id), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        return FileResponse(resolve_asset_path(project_id, asset))

    @app.post("/api/projects/{project_id}/assets/{asset_id}/analyze", response_model=Project)
    async def analyze_asset(project_id: str, asset_id: str) -> Project:
        project = get_project(project_id)
        asset = next((item for item in project.assets if item.id == asset_id), None)
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        if asset.kind not in {"character", "setting", "prop", "style", "image", "video"}:
            raise HTTPException(status_code=422, detail="Vision inspection requires image or video media")
        source_path = resolve_asset_path(project_id, asset)
        try:
            image, mime_type = _vision_media(source_path)
            result = await director.inspect_image(
                image=image,
                mime_type=mime_type,
                purpose=f"{asset.kind} reference named {asset.name}",
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (DirectorError, ValueError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        asset.vision = VisionInspectionRecord(model=settings.llm_model, **result.model_dump())
        return store.save(project)

    @app.put(
        "/api/projects/{project_id}/assets/{asset_id}/name",
        response_model=AssetRenameResponse,
    )
    def rename_asset(
        project_id: str, asset_id: str, request: AssetNameRequest
    ) -> AssetRenameResponse:
        """Rename one Asset — the whole display name, and the one route that edits it.

        **The Director's chosen fix for the name leak (2026-08-22).** 9–10 prompts per populate
        roll contained the literal internal label `HarderFaster · multiview` — *"HarderFaster ·
        multiview screams into the polished metal stand."* The existing defence in
        `lay_out_shots` ("A name never shown cannot be echoed") only applies once an identity
        sheet is *promoted*, and nothing on the live project is. The Director ruled against
        hiding names from the model: *"we dont want to lose the models ability to identify
        assets its using or that could get bad. If the assets name is a problem then we could
        rename it. Renaming assets may be useful anyway as the HarderFaster image is a picture
        of a Woman named Lucy, the song i made the image for is Harder Faster."* Renaming that
        asset to `Lucy` turns the leak into correct prose.

        **The whole name, never an edit around the suffix.** ` · multiview` is appended by
        `generate_multiview` when it mints the child, and ` · edit` by the image-edit route;
        both are *derivations*, not decorations this route should preserve. A rename that kept
        them would leave the Director unable to remove the very label they are renaming to get
        rid of, which is the entire ask.

        Three refusals, each before anything is assigned, so a refused rename leaves the asset
        exactly as it was:

        * the asset is not in this project — 404, `replace_consistency_prompt`'s wording;
        * the name is empty after trimming (`ASSET_NAME_EMPTY`) — a name has no meaningful
          blank, unlike an anchor or a slot;
        * the name is longer than `ASSET_NAME_LIMIT`, measured after trimming
          (`ASSET_NAME_TOO_LONG`), which is `replace_consistency_prompt`'s bound check verbatim.

        **A duplicate name is not refused**, and that is a decision rather than an oversight.
        `models.assets_for_proposal` already documents and resolves the case — first occurrence
        in library order wins — so two assets sharing a name is a deterministic state this
        application handles, not the ambiguity `CHARACTER_SLOT_TAKEN` refuses. A slot is a
        *link* and two holders would make a tagged line point at two references; a name is a
        label, and citations do not travel on it.

        **What a rename does not touch, and it is said on the wire.** Citations resolve by id
        (`AssetCitation.asset_id`), so no shot can lose its reference to a rename. Prose already
        written — a shot's `prompt`, a reference label the Director typed per shot — keeps the
        old spelling, because those are words a person or a model wrote and no route edits them
        on a rename's behalf. `AssetRenameResponse.prompts` counts the shots in that state and
        the message names it, so "the rename did not work" cannot be the reading.

        Reference maps *are* re-derived, `replace_consistency_prompt`'s line and its argument:
        the name is **in** the map — `timeline.anchored_label` composes it into every tag line —
        so a rename changes what every citing shot's map says about this picture. Free to
        re-derive for the prose shots, recorded as stale for the rest.

        **Derived children keep their own names, and it is said on the wire (2026-08-23).** The
        ` · multiview` and ` · edit` suffixes are composed once, at the moment the child is
        minted, and stored — there is no live derivation to re-run. Renaming `HarderFaster` to
        `Lucy` therefore leaves `HarderFaster · multiview` spelling the old name in the library
        and, for an ` · edit` child, on the roster the model reads (`citable_assets` hides a
        multiview behind its source; it does not hide an edit). The Director's own fix worked
        because they renamed the *child*; the same gesture on the parent would not have. See
        `ASSET_RENAME_CHILDREN` for why this is reported rather than propagated.

        **The prose scan's length fence is named too**, both directions. Under
        `NAME_SCAN_MIN_LENGTH` the substring fallback stops considering the name at all —
        renaming to `Ora` quietly ends the prose half of `assets_for_proposal` for this picture.
        At or over it, a name that is a substring of ordinary English starts matching them —
        `Rain` is inside "grain" and "training". Neither breaks a plan, because citations resolve
        by id; both change what a *future* fill cites, which is why the route says so rather than
        refusing. `ASSET_RENAME_OVER_MATCHES`' count is measured against this plan's own prose:
        evidence from the material at hand, not a dictionary.

        Nothing renders, arms, queues or approves; `comfy` is not touched on any path.
        """
        project = get_project(project_id)
        asset = next((item for item in project.assets if item.id == asset_id), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail=ASSET_NAME_EMPTY)
        if len(name) > ASSET_NAME_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=ASSET_NAME_TOO_LONG.format(
                    name=asset.name, length=len(name), limit=ASSET_NAME_LIMIT
                ),
            )
        previous = asset.name
        # Counted *before* the write, against the old spelling, because that is the question
        # being answered: how much prose in this plan still says the word the Director is
        # renaming away from. `NAME_SCAN_MIN_LENGTH` is not applied — this is a report about
        # exact text, not the substring scan that has to defend itself against short names.
        prompts = sum(1 for shot in project.shots if previous and previous in shot.prompt)
        # Direct children only, and deliberately not the transitive tree: an edit of an edit is a
        # child of the picture it was edited from, and its name was composed from *that* one's.
        # Naming a grandchild here would claim this rename should have reached a string it was
        # never composed from.
        children = [item for item in project.assets if item.parent_id == asset.id]
        children_stale = sum(1 for item in children if previous and previous in item.name)
        # The prose scan's fence, measured on the name that is landing rather than the one
        # leaving. `assets_for_proposal` lowercases both sides, so this does too; the over-match
        # count excludes shots that already cite this asset, because those cite it *declared* and
        # the fallback never runs for them.
        scannable = len(name) >= NAME_SCAN_MIN_LENGTH
        lowered = name.casefold()
        prose_matches = (
            sum(
                1
                for shot in project.shots
                if lowered in shot.prompt.casefold()
                and not any(
                    citation.asset_id == asset.id for citation in shot.citations
                )
            )
            if scannable
            else 0
        )
        # Written onto the *stored* Asset rather than a rebuilt one, `replace_consistency_prompt`'s
        # rule: there is no construction site here where `path`, `source`, `parent_id` or
        # `prompt_id` could be defaulted away by an edit that was only ever about one string.
        asset.name = name
        maps = refresh_reference_maps(project)
        return AssetRenameResponse(
            project=store.save(project),
            name=name,
            previous=previous,
            prompts=prompts,
            maps=len(maps),
            children=len(children),
            children_stale=children_stale,
            scannable=scannable,
            prose_matches=prose_matches,
            message=ASSET_RENAME_APPLIED.format(
                previous=previous or "this asset",
                name=name,
                maps=ASSET_RENAME_MAPS.format(count=len(maps)) if maps else "",
                prompts=(
                    ASSET_RENAME_PROMPTS.format(count=prompts, previous=previous)
                    if prompts
                    else ""
                ),
                children=(
                    ASSET_RENAME_CHILDREN.format(
                        count=len(children),
                        stale=children_stale,
                        previous=previous or "the old name",
                    )
                    if children
                    else ""
                ),
                # One sentence or the other, never both: a name the scan skips cannot over-match,
                # and `prose_matches` is already zero in that case.
                scan=(
                    ASSET_RENAME_UNSCANNABLE.format(
                        name=name, length=len(name), minimum=NAME_SCAN_MIN_LENGTH
                    )
                    if not scannable
                    else ASSET_RENAME_OVER_MATCHES.format(
                        count=prose_matches, name=name
                    )
                    if prose_matches
                    else ""
                ),
            ),
        )

    @app.post("/api/projects/{project_id}/assets/upload", response_model=Project)
    async def upload_asset(
        project_id: str,
        file: Annotated[UploadFile, File()],
        name: Annotated[str, Form()],
        kind: Annotated[Literal["character", "setting", "prop", "style", "image", "audio", "video"], Form()] = "image",
    ) -> Project:
        project = get_project(project_id)
        suffix = Path(file.filename or "").suffix.lower()
        allowed_extensions = {
            "character": {".png", ".jpg", ".jpeg", ".webp"},
            "setting": {".png", ".jpg", ".jpeg", ".webp"},
            "prop": {".png", ".jpg", ".jpeg", ".webp"},
            "style": {".png", ".jpg", ".jpeg", ".webp"},
            "image": {".png", ".jpg", ".jpeg", ".webp"},
            "audio": {".wav", ".mp3", ".flac"},
            "video": {".mp4", ".mov", ".webm", ".mkv"},
        }
        if suffix not in allowed_extensions[kind]:
            raise HTTPException(status_code=415, detail=f"Unsupported {kind} asset file type")
        assets_dir = store.media_dir(project_id) / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        filename = _safe_filename(file.filename or "asset")
        target = assets_dir / f"{len(project.assets):03d}-{filename}"
        _copy_upload(file, target, settings.max_upload_bytes)
        project.assets.append(
            Asset(
                name=name.strip() or target.stem,
                kind=kind,
                path=target.relative_to(store.project_dir(project_id)).as_posix(),
            )
        )
        return store.save(project)

    @app.post(
        "/api/projects/{project_id}/assets/{asset_id}/multiview",
        response_model=RenderJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def generate_multiview(
        project_id: str, asset_id: str, request: MultiviewRequest
    ) -> RenderJob:
        project = get_project(project_id)
        source = next((item for item in project.assets if item.id == asset_id), None)
        if not source:
            raise HTTPException(status_code=404, detail="Asset not found")
        if source.kind not in MULTIVIEW_SUBJECTS or not source.path:
            raise HTTPException(status_code=422, detail=multiview_refusal())
        source_root = (
            store.media_dir(project_id).resolve()
            if source.source == "upload"
            else (settings.comfy_root / "output").resolve()
        )
        source_path = (
            (store.project_dir(project_id) / source.path).resolve()
            if source.source == "upload"
            else (source_root / Path(source.path)).resolve()
        )
        if source_root not in source_path.parents or not source_path.is_file():
            raise HTTPException(status_code=404, detail="Multiview source image was not found")
        upload_name = f"mvp_{project_id}_{source.id}{source_path.suffix.lower()}"
        content_type = "image/png" if source_path.suffix.lower() == ".png" else "image/jpeg"
        try:
            uploaded = await comfy.upload(upload_name, source_path.read_bytes(), content_type)
            image_name = "/".join(
                part for part in (uploaded.get("subfolder", ""), uploaded["name"]) if part
            )
            child = Asset(
                name=f"{source.name} · multiview",
                # The sheet is the same subject as what it was promoted from, so the child
                # carries the source's kind. For a character that is exactly what this line
                # said before — character in, character out — so no sheet already in a
                # manifest means anything different than it did. For a ship it is the whole
                # point: promotion must not be the step that files a prop as a person.
                #
                # Nothing downstream reads this for a decision that could change: the H3
                # reference adapter buckets every non-audio, non-video kind to "picture",
                # and shot attachment does not filter by kind at all.
                kind=source.kind,
                path="",
                source="krea-multiview",
                parent_id=source.id,
                prompt=request.prompt,
                # **The sheet inherits its source's appearance anchor.** A multiview
                # promotion is the one child relationship in this application that promises
                # the child depicts *the same subject unchanged* — that is what a turnaround
                # sheet is, and `kind` is already inherited on that reasoning. The sheet is
                # then the asset shots actually cite, so an anchor that stopped at the parent
                # would be an anchor no render ever sees. It is a copy and not a link: the
                # Director may correct one without the other, and a link would make editing
                # a source silently rewrite what every shot citing the sheet is conditioned
                # with.
                #
                # Contrast `edit_asset` below, which deliberately does not inherit.
                #
                # `character_slot` is deliberately **not** inherited, and that is the opposite
                # call for the opposite reason. A slot is not a description of the subject, it is
                # an identity the sheet would then hold *alongside* its source — two assets in
                # one slot, which `replace_character_slot` refuses by name precisely because a
                # tagged line pointing at two references renders by accident. Nothing is lost by
                # leaving it: a citation of the source resolves to this sheet through
                # `prefer_identity_sheets` anyway, so the slot goes on naming the subject and the
                # substitution goes on naming the picture, which is the division those two rules
                # already have.
                consistency_prompt=source.consistency_prompt,
            )
            payload = build_multiview_payload(
                image_name=image_name,
                prompt=request.prompt,
                seed=request.seed,
                prefix=f"music-video-producer/{project_id}/assets/{child.id}-multiview",
            )
        except ComfyError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # The record first, then the graph, for `generate_flux`'s reason. The upload above
        # stays outside it: it puts a file in ComfyUI's input directory and costs no GPU
        # time, and its own failure is the same 502 it always was, with nothing recorded.
        job = RenderJob(
            kind="multiview",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id=child.id,
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
        child.prompt_id = submission.prompt_id

        # Onto a re-read, for `generate_flux`'s reason and by the same rule: the child is new,
        # so it is appended to whatever manifest is current rather than to the copy this route
        # read before the submission. See `record_submission`.
        def add_the_child(fresh: Project) -> None:
            fresh.assets.append(child)

        record_submission(project_id, job, patch=add_the_child)
        return job

    @app.post(
        "/api/projects/{project_id}/assets/{asset_id}/edit",
        response_model=RenderJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def edit_asset(
        project_id: str, asset_id: str, request: AssetEditRequest
    ) -> RenderJob:
        """AI Mod: one image asset plus one instruction becomes a *new* asset beside it.

        The Director's stage-3 ask, verbatim shape: prompt an edit, get a new image asset
        to keep, delete (rejection is ordinary deletion), or modify further — a child of
        an edit is an ordinary image asset, so edits chain. The source is never touched.

        The instruction travels in the workflow's own prompting form via
        `image_edit_prompt` — identity preserved, the edit stated, everything else kept —
        unless it already carries the structured marker, in which case the Director wrote
        the full form and it goes verbatim. The media reaches ComfyUI the reference
        path's way: a resolved absolute file path through the H3 media loader, no upload.

        `generate_multiview` is the template for everything else here: the child is
        created before submission with an empty path, the job (kind `edit`) targets it,
        and `apply_job_history` — the one completion writer — adopts the landed file.
        """
        project = get_project(project_id)
        source = next((item for item in project.assets if item.id == asset_id), None)
        if not source:
            raise HTTPException(status_code=404, detail="Asset not found")
        if source.kind in ("audio", "video"):
            raise HTTPException(
                status_code=422,
                detail=f"AI Mod edits images, and {source.name} is {source.kind} media.",
            )
        if not source.path:
            raise HTTPException(
                status_code=422,
                detail=f"{source.name} has no image yet — render or upload it first.",
            )
        if not request.instruction.strip():
            raise HTTPException(
                status_code=422,
                detail="Describe the edit: what should change, and what must stay.",
            )
        source_path = resolve_asset_path(project_id, source)
        prompt = image_edit_prompt(
            request.instruction,
            source_kind=source.kind,
            source_label=source.name,
        )
        child = Asset(
            name=f"{source.name} · edit",
            # An edited character is still a character; an edited setting is still a
            # setting. The multiview promotion's rule, for the multiview promotion's
            # reason.
            kind=source.kind,
            path="",
            source="h3-image-edit",
            parent_id=source.id,
            prompt=prompt,
            # **An edit does NOT inherit the source's appearance anchor**, and that is the
            # opposite decision to `generate_multiview` above on purpose. An anchor is an
            # assertion about what a subject looks like; an AI Mod is the act of changing
            # what it looks like ("put her in the black coat instead"). Copying the anchor
            # onto the child would carry a description the edit was run to invalidate, and
            # would carry it *silently* into every tag line and expansion citing the new
            # asset — the exact "plausible and wrong" failure this codebase keeps refusing.
            #
            # So the child starts with no anchor, which means "no anchor stored" and produces
            # the bare label everywhere, and the Director writes one for the edited look if
            # they want one. Nothing is lost: the source keeps its own, and this route never
            # touches the source.
        )
        try:
            payload = build_h3_image_edit_payload(
                prompt=prompt,
                pictures=[{"file": str(source_path), "label": source.name}],
                seed=request.seed,
                profile=request.profile,
                prefix=f"music-video-producer/{project_id}/assets/{child.id}-edit",
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        # The record first, then the graph, for `generate_flux`'s reason, and the child asset
        # appended only once the graph is accepted for the same one.
        job = RenderJob(
            kind="edit",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id=child.id,
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
        child.prompt_id = submission.prompt_id

        # Onto a re-read, for `generate_flux`'s reason and by the same rule: the child is new,
        # so it is appended to whatever manifest is current rather than to the copy this route
        # read before the submission. See `record_submission`.
        def add_the_child(fresh: Project) -> None:
            fresh.assets.append(child)

        record_submission(project_id, job, patch=add_the_child)
        return job

    @app.post(
        "/api/projects/{project_id}/assets/fill",
        response_model=AssetFillResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def fill_assets(project_id: str, request: AssetFillRequest) -> AssetFillResponse:
        """The Stage Manager (stage 3 of the Director's user workflow): assess and create.

        One model pass over the whole project proposes the supporting image assets the
        library still lacks; each proposal queues an ordinary Flux render through the
        exact asset shape `generate_flux` creates, so a landed proposal is
        indistinguishable from a hand-generated asset — keep it, delete it to reject,
        AI Mod it onward. The count is guidance to the model and a hard truncation here.

        Refused while renders are open, deliberately and with FR-9's number: Flux
        interleaved into an H3 batch evicts the resident stack at ~150 s per eviction.
        The GPU acknowledgement is server-enforced like every expensive path's.
        """
        project = get_project(project_id)
        if reconcilable_jobs(project):
            raise HTTPException(status_code=409, detail=ASSET_FILL_RENDERS_OPEN_REFUSAL)
        if not request.confirm_gpu:
            raise HTTPException(
                status_code=422,
                detail=ASSET_FILL_CONFIRM_REFUSAL.format(count=request.count),
            )
        context = project.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
        try:
            result = await director.stage_manager(
                project_context=context, count=request.count
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        proposals = [item for item in result.assets if item.prompt.strip()][: request.count]
        if not proposals:
            raise HTTPException(
                status_code=502,
                detail=ASSET_FILL_NO_PROPOSALS_REFUSAL.format(
                    message=(result.message or "").strip()[:300] or "(empty)"
                ),
            )
        # Re-read after the await, and re-check the eviction guard: an H3 batch submitted
        # while the model thought must not get Flux interleaved into it.
        project = get_project(project_id)
        if reconcilable_jobs(project):
            raise HTTPException(status_code=409, detail=ASSET_FILL_RENDERS_OPEN_REFUSAL)
        submitted: list[AssetFillSubmission] = []
        # What actually queued, in submission order, and the two things each one owns: the Asset
        # it creates and the record that will answer for it. Kept apart from `project` because
        # neither is written onto that object any more — every commit below lands on a re-read.
        landed: list[tuple[Asset, RenderJob]] = []

        def commit_the_accepted_assets(fresh: Project) -> None:
            """The Assets of the graphs that went out, onto whatever manifest is current.

            Used by both endings. A partial batch commits exactly this much and no more: the
            proposals whose graphs ComfyUI took, with the records for the rest settled beside
            them in the same write.
            """
            fresh.assets.extend(asset for asset, _record in landed)

        # Every record first, then every graph (the Director's 2026-08-21 ruling). One save
        # covers the whole batch rather than one per proposal: the property the ruling is
        # about is that a save race is answered *before* any GPU time is spent, and a batch
        # whose records could not be written spends none at all.
        pending: list[tuple[Asset, dict[str, Any], RenderJob]] = []
        for index, proposal in enumerate(proposals):
            asset = Asset(
                name=proposal.name,
                kind=proposal.kind,
                path="",
                source="stage-manager",
                prompt=proposal.prompt,
            )
            payload = build_flux_payload(
                prompt=proposal.prompt,
                width=1024,
                height=1024,
                steps=20,
                guidance=4.0,
                # Distinct seeds so two similar proposals cannot land the identical image.
                seed=index,
                prefix=f"music-video-producer/{project_id}/assets/{asset.id}",
            )
            job = RenderJob(
                kind="flux",
                prompt_id=PENDING_SUBMISSION_PROMPT_ID,
                target_id=asset.id,
                seed=index,
            )
            project.jobs.append(job)
            pending.append((asset, payload, job))
        store.save(project)
        for index, (asset, payload, job) in enumerate(pending):
            try:
                submission = await comfy.submit(payload)
            except ComfyError as error:
                # Partial batches are reported honestly: what queued is queued, and the
                # failure names itself; nothing already submitted is rolled back. The records
                # for the graphs that never went out — this one and every one after it — are
                # settled rather than left open, which is also what writes the accepted half
                # of the batch to disk.
                settle_unsubmitted_jobs(
                    project_id,
                    *(entry[2] for entry in pending[index:]),
                    accepted=[record for _asset, record in landed],
                    patch=commit_the_accepted_assets,
                )
                raise HTTPException(status_code=502, detail=str(error)) from error
            accept_submission(job, submission.prompt_id)
            asset.prompt_id = submission.prompt_id
            landed.append((asset, job))
            submitted.append(
                AssetFillSubmission(
                    asset_id=asset.id, name=asset.name, kind=asset.kind, job_id=job.id
                )
            )
        # The whole batch, onto a re-read. This loop holds the manifest across as many `/prompt`
        # round trips as there are proposals, which is the widest submission window in the
        # application; saving `project` at the end of it would revert every one of those
        # seconds. See `record_submission`.
        record_submission(
            project_id,
            *(record for _asset, record in landed),
            patch=commit_the_accepted_assets,
        )
        return AssetFillResponse(message=result.message, submitted=submitted)
