"""The project itself: the list, one project, its documents and its sections.

Everything under `/api/projects` that is about the project as a whole rather than about a
song, a shot, an asset or a render -- except `sections/fill-looks`, which stays in `app.py`
because a test patches `plan_fingerprint` in that module's namespace.

The generic `PUT` is here. It arrived when the asset-name enumeration that counted its writes
stopped counting them in one file and started counting them across the package; it re-adopts
the stored `Asset.name` rather than writing one, and that adoption is one of the two
assignments that enumeration allows anywhere in `src/music_video_producer/`.
"""

from __future__ import annotations

import shutil

from fastapi import HTTPException, status

from ..app import (
    DEFAULT_SETTING_NOT_A_SETTING,
    DELETE_PROJECT_CONFIRM,
    DOCUMENT_LABELS,
    DOCUMENT_RESTORE_REFUSAL,
    PROJECT_CHANGED_REFUSAL,
    DefaultSettingRequest,
    DocumentName,
    ProjectCreate,
    ProjectDocumentsRequest,
    SectionListRequest,
    _adopt_expansion_maps,
    _adopt_job_measurements,
    _adopt_shot_effects,
    _adopt_song_analysis,
    _adopt_song_recovery_slots,
    _adopt_song_vocal_type,
    _detach_song_analysis,
    _detach_song_recovery_slots,
    _require_approval_unchanged,
    _require_in_flight_status_kept,
    _require_song_replacement_confirmation,
    document_restore_notice,
    legal_sections,
    refresh_reference_maps,
)
from ..models import Project, TreatmentMessage
from .context import RouterContext


def register(ctx: RouterContext) -> None:
    """Register every route this module owns on the application it was handed.

    The context is unpacked into plain locals first -- `app` among them -- so
    every route below is registered by the same decorator, and closes over the
    same names, as it did when it was nested inside `create_app`. The move is
    the whole diff.
    """
    app = ctx.app
    discovered_looks = ctx.discovered_looks
    get_project = ctx.get_project
    get_project_for_update = ctx.get_project_for_update
    store = ctx.store

    @app.get("/api/projects", response_model=list[Project])
    def list_projects() -> list[Project]:
        return store.list()

    @app.post("/api/projects", response_model=Project, status_code=status.HTTP_201_CREATED)
    def create_project(request: ProjectCreate) -> Project:
        return store.create(Project(name=request.name.strip()))

    @app.get("/api/projects/{project_id}", response_model=Project)
    def read_project(project_id: str) -> Project:
        return get_project(project_id)

    @app.put("/api/projects/{project_id}/documents", response_model=Project)
    def replace_documents(project_id: str, request: ProjectDocumentsRequest) -> Project:
        project = get_project(project_id)
        project.creative_brief = request.creative_brief
        project.treatment = request.treatment
        project.style_bible = request.style_bible
        # A lock stops the *Director* from replacing a document; it does not stop the human
        # who set it from typing in the textarea, so the text above is assigned either way.
        # Refusing an edit here would leave the Director unable to fix a locked document
        # without unlocking, saving, editing, and locking again.
        if request.treatment_locked is not None:
            project.treatment_locked = request.treatment_locked
        if request.style_bible_locked is not None:
            project.style_bible_locked = request.style_bible_locked
        return store.save(project)

    @app.post("/api/projects/{project_id}/documents/{document}/restore", response_model=Project)
    def restore_document(project_id: str, document: DocumentName) -> Project:
        """Swap a document with its single kept previous version. No Director call.

        Recovery has to be reachable without the model: the failure it exists for is the
        Director returning something unwanted, and asking that same Director to undo it
        risks a second unwanted rewrite. This route reads and writes stored text only.

        The swap is normally symmetric, so the operation is its own inverse and a mis-click
        is recoverable — but not when the document being displaced is empty, because an
        empty slot has to refuse. That case is real and is the one where the recovered text
        matters most, so it is reported as one-way rather than promised reversible.

        A locked document may still be restored: a lock stops the Director, not the human
        who set it, exactly as `PUT /documents` still accepts hand edits to a locked
        document. `DOCUMENT_LOCK_NOTICE` states that scope, and a route test pins it.

        An empty slot refuses with 409 rather than silently blanking the live document with
        "" — the exact data loss AD-14 exists to stop.

        **The write is a compare-and-swap**, because a swap read from a stale copy is the same
        data loss by a different door. There is exactly one kept version, and this route both
        reads it and writes it: two restores that overlap swap the same pair twice and leave the
        document where it started with the slot spent, and a restore overlapping a chat turn
        writes the pre-turn text back over a document the Director had just accepted. Neither is
        detectable afterwards — both leave a well-formed pair of strings. `save`'s
        `if_generation` refuses on the generation this read was taken at, so the loser is told
        `SAVE_RACE_REFUSAL` by `handle_save_race` and can look before clicking again. Refused
        rather than retried, on `RENDER_STATUS_SAVE_ATTEMPTS`' own rule: a retry belongs to a
        loop that re-derives its verdict, and this one is a Director's click on a specific pair
        of documents they can see.
        """
        project, generation = get_project_for_update(project_id)
        previous = getattr(project, f"{document}_previous")
        if not previous.strip():
            raise HTTPException(
                status_code=409,
                detail=DOCUMENT_RESTORE_REFUSAL.format(document=DOCUMENT_LABELS[document]),
            )
        displaced = getattr(project, document)
        setattr(project, f"{document}_previous", displaced)
        setattr(project, document, previous)
        # Recorded in the thread, not only toasted: the chat is the audit trail of what
        # happened to these documents, and a restore is as much a change as a replacement.
        project.messages.append(
            TreatmentMessage(
                role="system",
                content=document_restore_notice(document, reversible=bool(displaced.strip())),
            )
        )
        return store.save(project, if_generation=generation)

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str, confirm_delete: bool = False) -> dict[str, str]:
        """Remove one project — manifest and media directory — for good.

        The gap the analyst named (2026-08-20): a night of experiments accumulates
        projects the switcher can never shed; eighteen had to be deleted by hand at the
        store level the same night. The confirmation flag is the song-replacement idiom:
        the first call without it is refused with the sentence naming what will be lost,
        so no client can delete by accident of a stray request.

        Takes rendered into ComfyUI's output tree are NOT touched: they live outside the
        project directory, other projects may reference study copies of them, and disk is
        the Director's to prune. Only the manifest and the project's own media go.
        """
        project = get_project(project_id)  # 404 before any confirmation talk
        if not confirm_delete:
            raise HTTPException(
                status_code=409,
                detail=DELETE_PROJECT_CONFIRM.format(
                    name=project.name, shots=len(project.shots)
                ),
            )
        shutil.rmtree(store.project_dir(project_id))
        return {"deleted": project_id}

    @app.put("/api/projects/{project_id}/default-setting", response_model=Project)
    def replace_default_setting(
        project_id: str, request: DefaultSettingRequest
    ) -> Project:
        """Declare which library setting is this video's location — the one writer of it.

        The Director's report (2026-08-20): on a 30-shot plan whose brief specifies a location,
        the setting asset was cited by 5 shots, because whether a shot carried its environment
        reference depended entirely on whether the model happened to spell the asset's display
        name into prose. This is the half of the fix that does not depend on a model:
        `populate` gives the declared location to every new shot that named no location of its
        own, and names it in the instruction so the model has a chance to name it first.

        **Explicit, and therefore refusable and reversible.** Nothing infers this field — no
        route derives it from a library that happens to hold one setting, no tool schema exposes
        it to a model, and the generic full-project `PUT` re-adopts the stored value rather than
        trusting a body (`replace_consistency_prompt`'s rule, for the reason that route's
        docstring gives). An empty `asset_id` clears it, which is what "no location" means, and
        an unset field is a genuine no-op: `populate` writes exactly the citations it wrote
        before this existed.

        What it does *not* do is touch a single existing shot. It is a declaration about the
        project, read by the next plan; a sweep over a plan the Director already has is the
        silent bulk edit this codebase's report-then-confirm convention exists to forbid.
        """
        project = get_project(project_id)
        asset_id = request.asset_id.strip()
        if not asset_id:
            project.default_setting_id = ""
            return store.save(project)
        asset = next((item for item in project.assets if item.id == asset_id), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        if asset.kind != "setting":
            raise HTTPException(
                status_code=422,
                detail=DEFAULT_SETTING_NOT_A_SETTING.format(
                    name=asset.name, kind=asset.kind
                ),
            )
        project.default_setting_id = asset_id
        return store.save(project)

    @app.put("/api/projects/{project_id}/sections", response_model=Project)
    def replace_sections(project_id: str, request: SectionListRequest) -> Project:
        """The Director's section marks: Intro/Verse/Chorus/Bridge/Outro windows + prompts.

        Sorted by start on write so every reader walks them in time order, refused on
        overlap because a moment of the song belonging to two sections makes both the
        shot→section mapping and the lyric-block pairing ambiguous. Gaps are legal — an
        unmarked stretch simply has no section, and everything downstream treats that as
        unknown rather than inventing coverage.
        """
        project = get_project(project_id)
        # `legal_sections` since 2026-08-23 — this route's own rule, extracted so the generic
        # `PUT /api/projects/{id}` cannot go on writing the same field to a different standard.
        project.sections = legal_sections(request.sections)
        return store.save(project)

    @app.put("/api/projects/{project_id}", response_model=Project)
    def replace_project(
        project_id: str, project: Project, confirm_song_replacement: bool = False
    ) -> Project:
        # With the write generation, for `replace_shots`' reason and on the same argument: the
        # revision token below is compared here, at the top, and this route rewrites the whole
        # manifest at the bottom. Anything that lands between the two passes the token and is
        # then reverted by the save — the widest such window in the application, because this
        # body carries every field of every Shot, Asset, job, section and document at once.
        current, generation = get_project_for_update(project_id)
        if project.id != project_id:
            raise HTTPException(status_code=422, detail="Project ID cannot be changed")
        if project.updated_at != current.updated_at:
            raise HTTPException(status_code=409, detail=PROJECT_CHANGED_REFUSAL)
        # This is the normal save path for every edit in the UI, so it cannot be gated on
        # carrying a Song — that would refuse ordinary saves. It is gated on *changing* one:
        # a body whose Song differs from the stored Song is a replacement or a removal
        # however it arrived, and without this the guard was one HTTP call wide of true.
        # `Song` has no timestamps, so an untouched Song round-trips equal and passes here;
        # both being None is equal too, and adding a first Song to a Song-less project is
        # not a replacement.
        #
        # The Song's recovery slots are taken off the stored song first, for the reason
        # `_adopt_song_recovery_slots` gives — and it has to happen ahead of this comparison,
        # or a client that predates the slots would send `None` for both, compare unequal, and
        # be told an ordinary save is a song replacement.
        _adopt_song_recovery_slots(project.song, current.song)
        # The declared vocal type is server-owned here for the *sixth* time this exact hole has
        # been found in this exact route, and it fails the same two ways `_adopt_song_recovery_slots`
        # describes. A client written before the field existed omits it, so an ordinary save
        # arrives carrying `"unstated"` and would silently un-declare the Director's cast — and a
        # body that *invents* one would be declaring a duet nobody declared, which is the
        # fabricated value this codebase refuses. `PUT .../song/vocal-type` is its one writer.
        #
        # Ahead of the comparison below for `_adopt_song_recovery_slots`' own reason: a body that
        # differs only here must compare equal, or an ordinary save from an old client would be
        # told it is replacing the song.
        _adopt_song_vocal_type(project.song, current.song)
        # And the envelope pointer, the **twelfth** recorded time this exact hole has been found
        # in this exact route. Same hole, same two failures, same position — ahead of the
        # comparison, or a client that predates the field sends a default `SongAnalysis`,
        # compares unequal, and is told an ordinary save is a song replacement. See
        # `_adopt_song_analysis`.
        #
        # **This said "the seventh field to need this in this route" from 2026-08-24 until
        # 2026-08-27, and it made the route's own ledger unreadable.** Seven was already taken by
        # `character_slot` (2026-08-21), so the sequence read 3,4,5,6,7,7,8,9,10,11,12,13 — two
        # different fields answering to one number, in the one place this repository counts a
        # defect it keeps re-meeting. This guard landed 2026-08-24, after the eleventh
        # (2026-08-23) and before the Effect Stack (2026-08-25), so twelfth is where it falls and
        # the two below moved up by one. The ordinals are chronological by when the hole was
        # *found*, which is the only ordering that makes "the Nth time" mean anything; every one
        # was dated from `git log -S` on its own comment before this renumbering, not inferred
        # from its position in the file.
        _adopt_song_analysis(project.song, current.song)
        if project.song != current.song:
            _require_song_replacement_confirmation(current, confirm_song_replacement)
            # Confirmed: this is a different song, so nothing kept for the old one comes with
            # it. Only reached once the gate above has let the replacement through, so a
            # refused save has cleared nothing.
            _detach_song_recovery_slots(project.song)
            # And the cast goes with the track it described. A vocal type carried across a
            # confirmed replacement would say the new song is a duet on the strength of the old
            # one's declaration — `_detach_song_recovery_slots`' argument exactly, and the reason
            # `import_song` builds a fresh `Song` whose default says the same thing.
            if project.song is not None:
                project.song.vocal_type = "unstated"
            # And the measurement goes with the track it measured. The fingerprint would report
            # the old envelope absent anyway, but writing a pointer already known to be wrong and
            # relying on a later read to notice is not how this codebase detaches derived state.
            _detach_song_analysis(project.song)
        # Render state and approval are the dedicated routes', not a save's. Both gates compare
        # the body against the stored Shot and refuse only a *difference*, so an ordinary save --
        # which round-trips both fields on every Shot -- is untouched. After the Song gate rather
        # than before it, so a body that changes both still gets the Song's refusal it always got.
        _require_in_flight_status_kept(current, project.shots)
        _require_approval_unchanged(current, project.shots)
        # The recovery slots and the document locks are server-owned, and this route binds a
        # whole client-supplied `Project` whose every field is defaulted. A body that simply
        # omits them — which is what any client written before they existed sends — arrives
        # as ""/False, so trusting it lets one ordinary save clear both kept versions and
        # unlock both documents: exactly what AD-14 and the lock exist to prevent. Worse, a
        # body that *invents* a slot would be planting text that the restore route then swaps
        # into the live document as "the version you had before". Only an applied Director
        # replacement writes a slot, and only `PUT /documents` sets a lock.
        for field in DOCUMENT_LABELS:
            for owned in (f"{field}_previous", f"{field}_locked"):
                setattr(project, owned, getattr(current, owned))
        # The thread is server-owned for the same reason and by the same argument. Nothing in
        # this application posts a message: the chat route, the expansion route and the restore
        # route are the only writers, and each appends exactly what it did. A client body is
        # therefore never the authority on it — and since a message now carries structured
        # notices, trusting one would let an ordinary save invent a refusal that never happened,
        # reword the reason a real one gave, or simply omit the field and revert every notice in
        # the project to undifferentiated prose. The recovery slots were the first case of this;
        # a body that merely *omits* what it does not know about is the shape of all of them.
        project.messages = current.messages
        # Every Asset's appearance anchor is server-owned here, for the third time this exact
        # hole has been found in this exact route. `consistency_prompt` is a defaulted `str`,
        # so a body that simply omits it — which is what every client written before it
        # existed sends, and what any hand-rolled API call sends — arrives as `""` and one
        # ordinary save would blank the Director's own text on every asset at once. Adopting
        # the stored value by id means this route cannot write the field in either direction:
        # the dedicated `PUT .../consistency-prompt` is its one writer, which is also what
        # keeps it out of reach of anything a model can call.
        #
        # An asset in the body that the stored project does not hold gets `""` rather than
        # whatever it carried, by the same rule: an anchor that arrived on this route was not
        # set by the Director on the route that sets anchors.
        stored_anchors = {asset.id: asset.consistency_prompt for asset in current.assets}
        for asset in project.assets:
            asset.consistency_prompt = stored_anchors.get(asset.id, "")
        # Every character slot is server-owned here on the identical argument, and it is the
        # *seventh* time. `character_slot` is a defaulted `int`, so a body that omits it — every
        # client written before it existed, every hand-rolled API call — arrives as `0`, and one
        # ordinary save would un-slot the whole cast at once and leave every `(S1)` in the lyric
        # sheet resolving to nothing. Adopting the stored value by id means this route cannot
        # write the field in either direction: `PUT .../character-slot` is its one writer, which is
        # also what keeps it out of reach of anything a model can call.
        #
        # An asset in the body that the stored project does not hold gets `0`, by the anchor's own
        # rule: a slot that arrived on this route was not set by the Director on the route that
        # sets slots.
        stored_slots = {asset.id: asset.character_slot for asset in current.assets}
        for asset in project.assets:
            asset.character_slot = stored_slots.get(asset.id, 0)
        # Every Asset's display name is server-owned here too, and this is the **eighth** time
        # this exact route has been the hole for an asset field. It is a different hazard from
        # the two above and worse in one direction: `name` is *required*, so no client omits it
        # — every client sends back whatever name it was holding, and a browser tab left open
        # across a rename reasserts the old one on its next ordinary save. That is a silent undo
        # of a decision the Director made on the route that makes it, and `PUT .../assets/{id}/name`
        # is that one door.
        #
        # `.get(asset.id, asset.name)` rather than the anchor's `.get(asset.id, "")`, and the
        # difference follows from the field: an asset the stored project does not hold has no
        # stored name to adopt, and blanking it would produce a library row nobody can read.
        # The body is the only source there, so the body is used there and nowhere else.
        stored_names = {asset.id: asset.name for asset in current.assets}
        for asset in project.assets:
            asset.name = stored_names.get(asset.id, asset.name)
        # The declared location is server-owned on the same argument, and it is the *fourth*
        # time this route has been the hole: `default_setting_id` is a defaulted `str`, so
        # every client written before it existed sends `""` and one ordinary save would clear
        # the Director's choice. `PUT .../default-setting` is its one writer, which also keeps
        # it out of reach of anything a model can call.
        project.default_setting_id = current.default_setting_id
        # The chosen sampling bundle is server-owned on the identical argument, and this is the
        # **tenth** recorded time this one route has been the hole for a field a narrower sibling
        # guards. `sampling_profile` is a defaulted `Literal`, so every client written before it
        # existed — and every hand-rolled API call — sends nothing, Pydantic fills `"default"`,
        # and one ordinary save would put a turbo project silently back on 20 steps: the
        # Director's choice of look undone by a rename. It fails the *other* way too, which is
        # the half a bare default would miss: a stale browser tab left open across a bundle
        # change reasserts the bundle it was holding, so the next Generate All spends hours of
        # GPU on a graph nobody selected. `PUT .../sampling-profile` is its one writer, which is
        # also what keeps it out of reach of anything a model can call.
        project.sampling_profile = current.sampling_profile
        # The recorded map is server-owned here for the fifth time this exact hole has been found
        # in this exact route. See `_adopt_expansion_maps`.
        _adopt_expansion_maps(project, {shot.id: shot for shot in current.shots})
        # The Effect Stack is server-owned here for the **thirteenth** recorded time this route
        # has been the hole for a field a narrower sibling guards, and this guard lands in the
        # same commit as the field rather than after the first save that eats one (AD-16). A body
        # carries every field of every Shot and `effects` is defaulted, so an ordinary save from
        # any existing client would blank every look in the project; a body that invented one
        # would write filter configuration past the catalogue validator that stands between a
        # client's numbers and a filter string. A Shot this project does not yet hold keeps the
        # stack it arrived with — Split and Duplicate are how one gets here — and it is validated
        # first, so the catalogue still answers before anything is stored. See
        # `_adopt_shot_effects`.
        _adopt_shot_effects(
            project, {shot.id: shot for shot in current.shots}, looks=discovered_looks
        )
        # Every recorded fact about a job is server-owned here — the *eleventh* and now the
        # *fourteenth* time this one route has been the hole for a field nothing else may write,
        # and the third time this particular helper has had to grow to close it. A body carries
        # every field of every job, and all of them are defaulted, so without this one ordinary
        # save from any existing client would blank the render costs, the sampling bundles *and*
        # the record of what every export looked like, while a body that invented any of them
        # would plant provenance for a render nobody ran or a grade nobody applied. This is the
        # **only** generic route a `RenderJob` can arrive on — `PUT .../shots` carries a
        # `ShotListRequest`, which has no `jobs` — and that is asserted rather than assumed by
        # `test_the_whole_project_put_is_the_only_route_a_job_record_can_arrive_on`, because the
        # thirteenth instance of this hole was a spec naming one of two routes. See
        # `_adopt_job_measurements`.
        _adopt_job_measurements(project, {job.id: job for job in current.jobs})
        # The generic write is the widest citation writer there is: a body carries every field of
        # every Shot *and* every Asset, so one save can move a citation, re-role one, rename a
        # reference, rename an asset, or remove one — and this is the recorded sibling-write hole
        # in this route three times over. Re-derived rather than trusted, and after every
        # server-owned field above has been re-adopted, so the map is built from the assets and
        # anchors that are actually being saved rather than from whatever the body claimed.
        refresh_reference_maps(project)
        # The section layer held to `PUT /sections`' own standard, and this is the **ninth**
        # recorded time this route has been the hole for a field a narrower sibling guards. It is
        # a different shape from the `_adopt_*` helpers above — sections are the Director's own
        # hand-dragged structure and this is where the browser saves them, so the stored value
        # cannot simply be taken back — but the failure is the same: a body this route accepts
        # unread is a body every later reader has to survive. Unsorted sections used to make
        # `timeline.layout_spans` emit *duplicate* spans, tiling the song twice, which raised
        # `SNAP_NESTED` out of populate as an unhandled 500 after a ~110 s model call.
        #
        # **Gated on *changing* the layer**, which is the Song guard's shape above and its
        # argument: this is the normal save path for every edit in the interface, so refusing an
        # ordinary save over a layer that was already on disk would make a project written before
        # this rule existed unsaveable — and unfixable, because the narrow route refuses the same
        # body. A save that round-trips the stored sections passes untouched; one that introduces
        # or edits an overlap is held to the rule that every reader of the field depends on.
        if project.sections != current.sections:
            project.sections = legal_sections(project.sections)
        return store.save(project, if_generation=generation)
