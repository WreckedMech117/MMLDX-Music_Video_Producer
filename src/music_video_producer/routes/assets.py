"""Assets: the library's images and the files this application serves back.

Five sibling routes are not here -- `upload`, `fill`, `multiview`, `edit` and `name` -- because
`tests/test_api.py` counts `Asset(` constructions and `asset.name = ` assignments in `app.py`
and would see a different number. `routes/__init__.py` names each one.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import FileResponse

from ..app import (
    CHARACTER_SLOT_NOT_A_CHARACTER,
    CHARACTER_SLOT_TAKEN,
    CONSISTENCY_PROMPT_LIMIT,
    CONSISTENCY_PROMPT_TOO_LONG,
    DELETE_ASSET_CITED,
    EXPANSION_LOCKED_NOTICE,
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
    AssetReplacementRequest,
    AssetReplacementResponse,
    AssetReplacementSkip,
    _replacement_row,
    _vision_media,
    refresh_reference_maps,
    shot_render_in_flight,
    shot_render_provenance,
)
from ..asset_replacement import asset_replacement_plan
from ..batch import shot_label
from ..director import DirectorError, DirectorUnavailable
from ..models import Project, VisionInspectionRecord, character_slot_assets
from ..timeline import ordered_shots
from ..workflows import H3_REFERENCE_LIMITS
from .context import RouterContext


def register(ctx: RouterContext) -> None:
    """Register every route this module owns on the application it was handed.

    The context is unpacked into plain locals first -- `app` among them -- so
    every route below is registered by the same decorator, and closes over the
    same names, as it did when it was nested inside `create_app`. The move is
    the whole diff.
    """
    app = ctx.app
    director = ctx.director
    get_project = ctx.get_project
    resolve_asset_path = ctx.resolve_asset_path
    settings = ctx.settings
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
