"""The project itself: the list, one project, its documents and its sections.

Everything under `/api/projects` that is about the project as a whole rather than about a
song, a shot, an asset or a render -- except the generic `PUT`, which stays in `app.py` because
the asset-name enumeration counts its writes there, and `sections/fill-looks`, which stays
because a test patches `plan_fingerprint` in `app.py`'s namespace.
"""

from __future__ import annotations

import shutil

from fastapi import HTTPException, status

from ..app import (
    DEFAULT_SETTING_NOT_A_SETTING,
    DELETE_PROJECT_CONFIRM,
    DOCUMENT_LABELS,
    DOCUMENT_RESTORE_REFUSAL,
    DefaultSettingRequest,
    DocumentName,
    ProjectCreate,
    ProjectDocumentsRequest,
    SectionListRequest,
    document_restore_notice,
    legal_sections,
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
