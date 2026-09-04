"""The routes no resource claims yet -- and the name says so on purpose.

`shots`, `assets`, `song`, `timeline`, `render` and `project` each own a path prefix, so a
route's file is guessable from its URL and the reverse. Nothing here is. That makes this
file the one place the split does not hold, and a route landing here is a question ("which
resource is this?") rather than a decision. It is called `unsorted` so that reading the
import list is enough to see it growing; a comfortable name would let it accrete quietly.

~~Five~~ **six** routes (`planning_turn` joined them on 2026-09-03, story 14.1), and `assemble`
and `readiness` would have been here too if tests were not holding them in `app.py` -- so the true
size of this tail is eight, not six. `assemble` is held by `trim_args` and `concat_args` being
patched in `music_video_producer.app`'s namespace, which is the class of pin no widened source
guard can lift.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException

from ..app import (
    ASSISTANT_APPLIED_NOTICE,
    ASSISTANT_DUPLICATE_NOTICE,
    ASSISTANT_EMPTY_FILL_NOTICE,
    ASSISTANT_EMPTY_MESSAGE,
    ASSISTANT_IDENTITY_SHEET_NOTICE,
    ASSISTANT_MALFORMED_EMPTY_NOTICE,
    ASSISTANT_MALFORMED_NOTICE,
    ASSISTANT_MISSING_TARGET_NOTICE,
    ASSISTANT_OMITTED_NOTICE,
    ASSISTANT_OUT_OF_SCOPE_NOTICE,
    ASSISTANT_SPECIFICATION_NOTICE,
    ASSISTANT_UNKNOWN_ASSET_NOTICE,
    ASSISTANT_WITHOUT_SHOTS,
    ASSISTANT_WITHOUT_TOOL_CALL_NOTICE,
    ASSISTANT_WITHOUT_WRITABLE_SHOTS,
    CHAT_EMPTY_MESSAGE,
    DIRECTOR_CONTEXT_EXCLUDE,
    DIRECTOR_REPLACEABLE_DOCUMENTS,
    DOCUMENT_LABELS,
    DOCUMENT_REJECTED_EMPTY_NOTICE,
    DOCUMENT_REJECTED_NOTICE,
    DOCUMENT_WRITER_MACHINE,
    EXPANSION_DUPLICATE_NOTICE,
    EXPANSION_EMPTY_MESSAGE,
    EXPANSION_LOCKED_NOTICE,
    EXPANSION_OMITTED_NOTICE,
    EXPANSION_REJECTED_EMPTY_NOTICE,
    EXPANSION_REJECTED_NOTICE,
    EXPANSION_RENDERED_NOTICE,
    EXPANSION_UNKNOWN_NOTICE,
    EXPANSION_WITHOUT_SHOTS,
    EXPANSION_WRITTEN_NOTICE,
    NOTICE_JOIN,
    PLANNING_EMPTY_MESSAGE,
    PLANNING_MALFORMED_EMPTY_NOTICE,
    PLANNING_MALFORMED_NOTICE,
    PLANNING_WITHOUT_CONSENT_NOTICE,
    PLANNING_WITHOUT_TOOL_CALL_NOTICE,
    SHOT_PLAN_EMPTY_NOTICE,
    SHOT_WINDOW_NOTICE,
    AssistantRequest,
    DirectorRequest,
    EffectCatalogueResponse,
    PlanningRequest,
    ShotExpansionOutcome,
    _short,
    apply_expansions,
    assistant_fill_summary,
    assistant_prompt_rejection,
    assistant_reply,
    document_change_notice,
    document_first_draft_notice,
    document_lock_refusal,
    document_not_requested_notice,
    effect_catalogue_report,
    expand_shots,
    expansion_rejection,
    expansion_shot_label,
    expansion_sweep_notices,
    planning_proposals_notice,
    planning_questions_notice,
    prose_claims_shots,
    refresh_reference_maps,
    rejection_notice,
    shot_claim_mismatch_notice,
    shot_render_provenance,
    shot_write_refusal,
    write_document,
)
from ..batch import shot_label
from ..director import DirectorError, DirectorUnavailable, document_rejection
from ..dp_prompt import DP_SYSTEM_PROMPT, dp_input
from ..models import (
    AssetCitation,
    MessageNotice,
    Project,
    Shot,
    TreatmentMessage,
    dangling_citations,
    identity_sheet_ids,
    mode_specification_problems,
    new_id,
    prefer_identity_sheets,
)
from ..timeline import (
    H3_MAX_SHOT_SECONDS,
    H3_MIN_SHOT_SECONDS,
    assistant_input,
    expansion_input,
    ordered_shots,
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
    settings = ctx.settings
    store = ctx.store

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "app": "Music Video Producer",
            "version": "0.1.0",
            "comfy": await comfy.health(),
            "llm": {
                "configured": bool(settings.llm_base_url and settings.llm_model),
                "model": settings.llm_model,
            },
        }

    @app.get("/api/effects/catalogue", response_model=EffectCatalogueResponse)
    def read_effect_catalogue(rescan: bool = False) -> EffectCatalogueResponse:
        """Every effect, its family, its parameters and their bounds — plus the looks on disk.

        Not project-scoped, because neither half is a fact about a video. The catalogue is code,
        and the looks belong to the *machine*: `{data_root}/luts/` is a sibling of `projects/`, on
        `preferences.py`'s precedent, because a manifest that carried its own colour science would
        grade differently on someone else's install.

        One read for both, so a picker is one request rather than two round trips for one panel.

        **The folder is not re-read to answer this.** `discovered_looks` holds what it found for
        the life of the process — 221 ms cold on a 44.2 MB pack — and `rescan=true` is the
        Director's explicit "I just added a look", never something the panel does when it opens.
        """
        return effect_catalogue_report(discovered_looks(rescan=rescan))

    @app.post("/api/projects/{project_id}/director/chat", response_model=Project)
    async def director_chat(project_id: str, request: DirectorRequest) -> Project:
        # This snapshot is only ever used to build the prompt. It carries the user's message
        # so the model sees the turn it is answering, and it is then thrown away — see the
        # re-read after the await.
        snapshot = get_project(project_id)
        snapshot.messages.append(TreatmentMessage(role="user", content=request.message))
        # The recovery slots are excluded, and that is not an optimisation. This dump is the
        # whole project, so leaving them in would echo a second full copy of every creative
        # document into every prompt — and the recorded root cause of the original document
        # corruption was degradation under rich context (JSON in context begets JSON), the
        # very failure `document_rejection` was written to catch. The locks stay: they are
        # two booleans, and knowing a document is off-limits is useful direction.
        context = snapshot.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
        try:
            result = await director.plan(message=request.message, project_context=context)
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # Re-read after the await. A local model can hold this call open for many seconds,
        # and anything committed in that window — a lock set, a restore applied, a document
        # hand-edited — would otherwise be silently reverted by the stale snapshot on save.
        # Every decision below reads the fresh state: the lock that says do not touch this,
        # the existing text the guard compares against, and the slot being spent.
        # Read with the write generation, so this turn's own save cannot become the thief. The
        # re-read above closes the long window -- the model call -- and this closes the short one
        # after it: another writer that lands between the read and the save would otherwise be
        # reverted by a whole-manifest write, silently, with both requests answering 200. The
        # asymmetry is what makes it worth the token: `PUT /shots` is refused when it loses this
        # race, so without it the same collision is refused one way and lost the other.
        project, generation = get_project_for_update(project_id)
        project.messages.append(TreatmentMessage(role="user", content=request.message))
        notices: list[MessageNotice] = []
        replaced: list[str] = []
        first_drafts: list[str] = []
        not_requested: list[str] = []
        # `DIRECTOR_REPLACEABLE_DOCUMENTS`, not `DOCUMENT_LABELS`: this loop asks *which
        # documents may an ordinary reply rewrite*, and since 2026-09-03 that is no longer the
        # same question as *which documents have a lock, a slot and a restore route*. The Brief
        # has the apparatus and is not here, because `DirectorResult` carries no text for it —
        # which is what the mapping is derived from, so this loop cannot reach for a field a
        # reply does not have.
        for field, label in DIRECTOR_REPLACEABLE_DOCUMENTS.items():
            candidate = getattr(result, field)
            existing = getattr(project, field)
            # A candidate identical to the stored text is not a replacement, whatever the
            # guard says about it — `document_rejection` returns "" for an echo. Spending
            # the single recovery slot on it would annihilate the genuinely recoverable
            # version with a copy of the live one, and announcing it would be a change the
            # Director cannot find. Nothing captured, nothing assigned, nothing claimed.
            if candidate.strip() == existing.strip():
                continue
            reason = document_rejection(candidate, existing)
            # The lock is checked after the comparisons but before anything is written, so
            # nothing is assigned and nothing is captured — the lock must not spend the
            # recovery slot on a replacement it refused to make. It is *reported* only when
            # the candidate would genuinely have changed something, or a project with a
            # locked Treatment would carry the same paragraph on every reply forever.
            # `document_lock_refusal` rather than the check written out here, so the one
            # question *may a machine write this document* has one answer across the chat
            # route and every planning pass that comes after it. See that function.
            if refusal := document_lock_refusal(project, field):
                if not reason:
                    notices.append(MessageNotice(kind="refusal", text=refusal))
                continue
            # Consent is the second "do not write, and say why" gate, and it sits *after* the
            # lock deliberately: a lock is durable state the Director set and a flag is one
            # turn, so when both apply "locked" is the sentence worth reading — and it must
            # keep saying locked rather than merely unrequested, or unticking the box would
            # quietly relabel a protection as an oversight.
            #
            # It carries the lock's silence rule for the same reason: a candidate the guard
            # would have refused anyway would not have landed with consent either, so
            # reporting it as merely unrequested would invite a retry that also refuses.
            if not request.apply_documents:
                if not reason:
                    not_requested.append(label)
                continue
            if reason:
                # The candidate travels in `raw`, never in the sentence. It used to be pasted
                # into `content` — the one place in this module guaranteed to be handed back
                # to the model on the next turn. `MessageNotice` is what bounds it.
                notices.append(
                    rejection_notice(
                        DOCUMENT_REJECTED_NOTICE,
                        DOCUMENT_REJECTED_EMPTY_NOTICE,
                        raw=candidate,
                        document=label,
                        reason=reason,
                    )
                )
                continue
            # Capture on apply, never on attempt. Writing the recovery slot before the
            # guard ran would let a rejected candidate overwrite the only copy of the good
            # document — turning a protective refusal into the data loss it prevents. That
            # ordering is this loop's; *what* the capture does is `write_document`'s, shared
            # with the Director's own save and with every other machine writer since 13.1.
            write_document(project, field, candidate, writer=DOCUMENT_WRITER_MACHINE)
            # A blank target accepts any first draft, by design, so the slot it captures is
            # empty and a restore would refuse. Reported separately: describing that as a
            # replacement whose previous version "can be restored" is a promise broken by
            # the very next click.
            (first_drafts if not existing.strip() else replaced).append(label)
        # Both statements go ahead of the "was NOT replaced" notices: what did change is what
        # the Director has to review, and it is the thing this reply used to never mention.
        if first_drafts:
            notices.insert(
                0, MessageNotice(kind="change", text=document_first_draft_notice(first_drafts))
            )
        if replaced:
            notices.insert(
                0, MessageNotice(kind="change", text=document_change_notice(replaced))
            )
        # One grouped statement rather than one per document: a declined turn wrote nothing, so
        # the Director needs the list and the reason once, not the same paragraph twice.
        if not_requested:
            notices.append(
                MessageNotice(kind="refusal", text=document_not_requested_notice(not_requested))
            )
        # A model that returned no sentence of its own must not leave the reply as a bare
        # separator followed by notices — the expansion route's guard, which this one lacked.
        message = result.message.strip() or CHAT_EMPTY_MESSAGE
        # The two empty-list notices are independent, and both can fire on one reply. They answer
        # different questions: this one says the reply contradicts itself, and the next says the
        # consent the Director gave produced nothing. Suppressing either would leave one of those
        # facts unsaid in exactly the turn it is about.
        if not result.shots and prose_claims_shots(message):
            notices.append(shot_claim_mismatch_notice(len(project.shots)))
        if request.apply_shots and not result.shots:
            notices.append(MessageNotice(kind="flag", text=SHOT_PLAN_EMPTY_NOTICE))
        for item in result.shots:
            if item.duration < H3_MIN_SHOT_SECONDS or item.duration > H3_MAX_SHOT_SECONDS:
                notices.append(
                    MessageNotice(
                        kind="flag",
                        text=SHOT_WINDOW_NOTICE.format(
                            duration=item.duration,
                            start=item.start,
                            minimum=H3_MIN_SHOT_SECONDS,
                            maximum=H3_MAX_SHOT_SECONDS,
                        ),
                    )
                )
        project.messages.append(assistant_reply(message, notices))
        if request.apply_shots and result.shots:
            # **Aligned to the Shots the model was shown, not to positions in a list read
            # afterwards.** `PlannedShot` carries no id, so position is the only correspondence
            # there is — and it is a correspondence with `snapshot`, the project as it stood when
            # the prompt was built. The re-read above exists so a document, a lock or a Shot
            # committed during a model call that runs for many seconds is not reverted, and that
            # same re-read is what makes a position in `project.shots` a different thing from a
            # position in `result.shots`: one Shot added, deleted or split while the model was
            # thinking shifts every index past it, and `start`, `duration` and `prompt` then land
            # on a Shot the model never described. A wrong window is at least visible; a
            # plausible prompt on the wrong Shot reads as intentional forever, which is the
            # argument `expand_shot_prompts` already makes for keying by id.
            #
            # A Shot the model described that no longer exists is dropped rather than recreated:
            # re-creating it would invent a window on a plan the Director has just changed, which
            # is the one thing this merge must not do. It is silent because the browser hardcodes
            # `apply_shots: false` — this path is API-only today, and a notice nobody can reach
            # would be a wire contract bought for nothing.
            described = [shot.id for shot in snapshot.shots]
            live = {shot.id: shot for shot in project.shots}
            added: list[Shot] = []
            for index, item in enumerate(result.shots):
                if index >= len(described):
                    added.append(
                        Shot(start=item.start, duration=item.duration, prompt=item.prompt)
                    )
                    continue
                shot = live.get(described[index])
                if shot is not None and not shot.locked:
                    shot.start = item.start
                    shot.duration = item.duration
                    shot.prompt = item.prompt
            # `live`'s values are the stored objects, so the writes above are already on the
            # plan; only the Shots the model added past the end of what it saw are new.
            project.shots = [*project.shots, *added]
        return store.save(project, if_generation=generation)

    @app.post("/api/projects/{project_id}/planning/turn", response_model=Project)
    async def planning_turn(project_id: str, request: PlanningRequest) -> Project:
        """One Treatment Planning turn: the assistant asks, writes the Brief, or proposes assets.

        Story 14.1, and the load-bearing decisions are next door in `director.py`: three tools with
        no optional fields, so *asked a question and wrote nothing* is a different call rather than
        a missing key (AD-38), and no field anywhere that could write the Treatment or the Style
        bible (TP-10). This route is what turns those into a stored turn, and four properties are
        load-bearing here rather than there:

        * **Consent is per request and is never remembered.** `request.apply_documents` is the only
          thing that lets the Brief be written, it is read from *this* body, and nothing about it
          is stored on the `Project` or inferred from the thread (AD-35). A second request with the
          flag off is refused however emphatically the first one carried it.
        * **The Brief's lock is Slice A's `document_lock_refusal`**, not a second implementation of
          *may a machine write this document*. Same question, same answer, same sentence as the one
          that refuses a Director reply — which is the whole reason that function was extracted.
        * **The write is `director_chat`'s document loop for one document**, in the same order and
          for the same recorded reasons: an echo of the stored text is not a replacement, the lock
          is reported before consent because a lock is durable state and a flag is one turn, and
          the recovery slot is captured on *apply* rather than on attempt so a refused candidate
          can never overwrite the only copy of the good document.
        * **AD-43**: what happened is `MessageNotice` entries on an ordinary `TreatmentMessage`.
          Nothing is announced by a convention inside `content`.

        Nothing here spends GPU time, touches `comfy`, sets a status, queues a job or promotes an
        asset — and nothing writes a proposal anywhere, because there is nowhere to write it until
        Slice F. `PLANNING_PROPOSALS_NOTICE` says so to the Director rather than leaving "3 assets
        proposed" to read like three assets appeared.

        The snapshot-then-re-read, the write generation, and the 503/502 mapping are
        `director_chat`'s, for the reasons documented there.
        """
        snapshot = get_project(project_id)
        snapshot.messages.append(TreatmentMessage(role="user", content=request.message))
        context = snapshot.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
        try:
            turn = await director.assist_planning(
                message=request.message, project_context=context
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        project, generation = get_project_for_update(project_id)
        project.messages.append(TreatmentMessage(role="user", content=request.message))
        notices: list[MessageNotice] = []
        # The reply's id, allocated before the write rather than by the append at the bottom.
        # A mark on the Brief names the turn that made it (AD-43), and the turn's own message
        # cannot be built until the write has happened — its notices are what the write
        # produces. So the id comes first and both halves are given the same one, which is what
        # makes *"which turn wrote this paragraph"* answerable from the stored project alone.
        reply_id = new_id("msg")
        label = DOCUMENT_LABELS["creative_brief"]
        # `turn.wrote_nothing()` rather than `not turn.brief`: the question this branch asks is
        # about the turn, and the empty string is an encoding of the answer rather than the answer.
        if not turn.wrote_nothing():
            existing = project.creative_brief
            # An echo of the stored text is not a replacement. Spending the single recovery slot on
            # it would annihilate the genuinely recoverable version with a copy of the live one,
            # and announcing it would be a change the Director cannot find.
            if turn.brief.strip() != existing.strip():
                reason = document_rejection(turn.brief, existing)
                refusal = document_lock_refusal(project, "creative_brief")
                if refusal:
                    if not reason:
                        notices.append(MessageNotice(kind="refusal", text=refusal))
                elif not request.apply_documents:
                    # Carries the lock's silence rule for its reason: a candidate the guard would
                    # have refused anyway would not have landed with consent either, so reporting
                    # it as merely unrequested would invite a retry that also refuses.
                    if not reason:
                        notices.append(
                            MessageNotice(
                                kind="refusal",
                                text=PLANNING_WITHOUT_CONSENT_NOTICE.format(document=label),
                            )
                        )
                elif reason:
                    # The candidate travels in `raw`, never in the sentence — `raw` is the field
                    # `DIRECTOR_CONTEXT_EXCLUDE` strips, so it is inspectable without being fed
                    # back to the model on the next turn.
                    notices.append(
                        rejection_notice(
                            DOCUMENT_REJECTED_NOTICE,
                            DOCUMENT_REJECTED_EMPTY_NOTICE,
                            raw=turn.brief,
                            document=label,
                            reason=reason,
                        )
                    )
                else:
                    # **The Brief's recovery slot is captured here, and that is a departure from
                    # Slice A worth stating.** `SAVE_CAPTURED_DOCUMENTS` puts the Brief in the
                    # save-captures group, derived from `DIRECTOR_REPLACEABLE_DOCUMENTS` on the
                    # argument that no reply can write the Brief so its own save is the only
                    # displacement there is. That argument was true until this route existed. The
                    # rule underneath it — *whichever writer is the threat fills the slot* — now
                    # names two writers for this document, and a machine write that displaced a
                    # Brief the Director spent an hour on without keeping a copy is precisely the
                    # loss the slot exists to prevent. Capture on apply, never on attempt.
                    #
                    # `attributed_to` is what makes the mark this write leaves point back at
                    # this turn. The reconciliation itself is `write_document`'s, run there for
                    # every writer of the Brief; nothing about it is decided here.
                    write_document(
                        project,
                        "creative_brief",
                        turn.brief,
                        writer=DOCUMENT_WRITER_MACHINE,
                        attributed_to=reply_id,
                    )
                    # A blank target accepts any first draft, so the slot it captures is empty and
                    # a restore would refuse. Reported separately, because describing that as a
                    # replacement whose previous version "can be restored" is a promise broken by
                    # the very next click.
                    wording = (
                        document_first_draft_notice
                        if not existing.strip()
                        else document_change_notice
                    )
                    notices.append(MessageNotice(kind="change", text=wording([label])))
        if turn.questions:
            notices.append(
                MessageNotice(kind="flag", text=planning_questions_notice(turn.questions))
            )
        if turn.proposals:
            notices.append(
                MessageNotice(
                    kind="flag",
                    text=planning_proposals_notice(
                        [_short(proposal.name) for proposal in turn.proposals]
                    ),
                )
            )
        if turn.malformed:
            notices.append(
                rejection_notice(
                    PLANNING_MALFORMED_NOTICE,
                    PLANNING_MALFORMED_EMPTY_NOTICE,
                    raw=NOTICE_JOIN.join(turn.malformed),
                    count=len(turn.malformed),
                )
            )
        # Prose and no tool call at all, which is a different fact from a question-only turn: one
        # is the model choosing the tool that writes nothing, the other is it reaching for no tool.
        if not notices and turn.wrote_nothing():
            notices.append(MessageNotice(kind="flag", text=PLANNING_WITHOUT_TOOL_CALL_NOTICE))
        message = turn.message.strip() or PLANNING_EMPTY_MESSAGE
        project.messages.append(assistant_reply(message, notices, message_id=reply_id))
        return store.save(project, if_generation=generation)

    @app.post("/api/projects/{project_id}/director/expand", response_model=Project)
    async def expand_shot_prompts(
        project_id: str, focus: Literal["story", "photography"] = "story"
    ) -> Project:
        """Turn the Treatment, Style Bible and timed Shot windows into a prompt per Shot (FR-26).

        A thin delegator over two pure things: `expansion_input` builds what the model sees, and
        `expansion_rejection` decides what may be written. Nothing here computes either, so both
        are testable without a route and the route can be asserted to pass the builder's output
        verbatim.

        Four properties are load-bearing:

        * **Keyed by shot id, never by position.** The chat route's positional merge is safe
          enough for start/duration, where a wrong assignment shows up as a visibly wrong
          window; a prompt is free text, so the same mistake after a concurrent add, delete or
          split would read as a plausible prompt forever and nothing downstream would fail.
        * **Nothing is rendered.** Expansion never touches `comfy`, never sets a Shot's
          `status`, and never queues a job. The prompt lands in the shot inspector, where the
          Director edits it and then decides about GPU time.
        * **No retiming.** `start`, `duration` and every window are untouched; only `prompt` is
          assigned.
        * **Only draft, unlocked Shots are written.** A lock is the Director's decision; render
          provenance is a fact about media that already exists. Both are reported rather than
          silently skipped, because "nothing happened to this Shot" has to say why.

        The empty-plan refusal, the re-read after the await, the 503/502 mapping and the single
        terminal `store.save` all follow `director_chat`.
        """
        # Built from the pre-await snapshot, exactly as the chat prompt is: this is what the
        # model sees, and it is then thrown away in favour of the re-read below.
        #
        # `focus` selects the persona over the identical machinery (2026-08-19): "story"
        # is pass one; "photography" is the DP pass the Director asked for after the
        # first full run's repeated setups — same whole-plan shape, same id-keyed apply,
        # same guards and notices, a different job description and a camera-trimmed input.
        snapshot = get_project(project_id)
        if not snapshot.shots:
            raise HTTPException(status_code=422, detail=EXPANSION_WITHOUT_SHOTS)
        photography = focus == "photography"
        try:
            # The kwarg travels only on the DP pass, so every existing `expand` double —
            # and the story pass's own call shape — stays byte-identical.
            result = await (
                director.expand(
                    expansion_input=dp_input(snapshot), system_prompt=DP_SYSTEM_PROMPT
                )
                if photography
                else director.expand(expansion_input=expansion_input(snapshot))
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # Re-read after the await, for the reason `director_chat` documents — and here it is
        # also what makes id-keying meaningful: a Shot added, deleted or split while the model
        # was thinking is in this project and not in the snapshot the result answers.
        #
        # With the write generation, and refused rather than laid over a newer manifest, for
        # `director_chat`'s reason exactly: the re-read closes the model call's window and the
        # token closes the one after it.
        project, generation = get_project_for_update(project_id)
        # Re-checked, not assumed from the snapshot: every Shot can be deleted while the model
        # is thinking, and saving a reply about a plan the pre-call guard would have refused
        # would leave the thread asserting an expansion of nothing.
        if not project.shots:
            raise HTTPException(status_code=422, detail=EXPANSION_WITHOUT_SHOTS)
        shots_by_id = {shot.id: shot for shot in project.shots}
        # Labelled by `shot_label`, which numbers by song order — the same order `expansion_input`
        # gives the model and the same order the timeline draws — so the notice, the model's input
        # and the clip are all talking about the same Shot under the same number. Still walked in
        # `ordered_shots` order so the notice reads down the song rather than down the manifest.
        labels = {
            shot.id: expansion_shot_label(project, shot) for shot in ordered_shots(project)
        }
        written: list[str] = []
        locked: list[str] = []
        rendered: list[str] = []
        unknown: list[str] = []
        duplicated: list[str] = []
        rejected: list[MessageNotice] = []
        answered: set[str] = set()
        for item in result.shots:
            shot = shots_by_id.get(item.shot_id)
            if shot is None:
                # Reported, not created and not guessed at. See EXPANSION_UNKNOWN_NOTICE. The
                # list is deduplicated because a model looping on one bad id would otherwise
                # repeat it through the whole notice.
                if item.shot_id not in unknown:
                    unknown.append(item.shot_id)
                continue
            # First answer wins, before any other check. Last-write-wins would let one Shot be
            # reported as refused *and* written in the same reply, and there is no reason to
            # prefer whichever contradiction arrived last.
            if shot.id in answered:
                if shot.id not in duplicated:
                    duplicated.append(shot.id)
                continue
            # Answered before the lock, provenance and rejection checks: the model did address
            # this Shot, so it is not an omission whatever happens to the prompt it sent.
            answered.add(shot.id)
            if shot.locked:
                locked.append(shot.id)
                continue
            # After the lock: both mean "not written", but a lock is a decision the Director
            # made and provenance is a fact about media, so when both apply the lock is the
            # sentence worth reading — the precedence `director_chat` uses for lock vs consent.
            if shot_render_provenance(shot):
                rendered.append(shot.id)
                continue
            reason = expansion_rejection(item.prompt)
            if reason:
                # The refused prompt is restored here, in `raw`. Story 2.2 dropped it because it
                # had nowhere to live that the next Director call would not read; the notice's
                # excluded field is that place, so the Director can now see what was refused.
                # `ExpandedShot.prompt` has no upper bound, so `MessageNotice` is what stops an
                # unbounded prompt reaching the manifest.
                rejected.append(
                    rejection_notice(
                        EXPANSION_REJECTED_NOTICE,
                        EXPANSION_REJECTED_EMPTY_NOTICE,
                        raw=item.prompt,
                        shot=labels[shot.id],
                        reason=reason,
                    )
                )
                continue
            shot.prompt = item.prompt
            written.append(shot.id)
        # A locked or already-rendered Shot the model never answered for is not an omission:
        # nothing was going to be written for it either way, and telling the Director to "run
        # expansion again if you want them written" would be advice that can never work.
        omitted = [
            shot.id
            for shot in project.shots
            if shot.id not in answered and not shot.locked and not shot_render_provenance(shot)
        ]
        notices: list[MessageNotice] = []
        # What changed goes first, as it does in the chat reply: it is the thing the Director has
        # to review, and everything below it is an explanation of something that did not happen.
        if written:
            notices.append(
                MessageNotice(
                    # The confirmation, and the one notice on this route that is good news:
                    # "Prompts written for 4 shot(s)" is the thing the Director pressed the
                    # button for, and dressing it as caution is how caution stops being read.
                    kind="change",
                    text=EXPANSION_WRITTEN_NOTICE.format(
                        count=len(written),
                        shots=", ".join(labels[shot_id] for shot_id in written),
                    ),
                )
            )
        # A lock and existing render provenance are decisions to *not* write; an omission and a
        # contradiction are the model behaving oddly about Shots nothing refused.
        for reported, wording, kind in (
            (locked, EXPANSION_LOCKED_NOTICE, "refusal"),
            (rendered, EXPANSION_RENDERED_NOTICE, "refusal"),
            (omitted, EXPANSION_OMITTED_NOTICE, "flag"),
            (duplicated, EXPANSION_DUPLICATE_NOTICE, "flag"),
        ):
            if reported:
                notices.append(
                    MessageNotice(
                        kind=kind,
                        text=wording.format(
                            shots=", ".join(labels[shot_id] for shot_id in reported)
                        ),
                    )
                )
        if unknown:
            notices.append(
                MessageNotice(
                    # Discarded rather than guessed at: a refusal to invent a Shot.
                    kind="refusal",
                    text=EXPANSION_UNKNOWN_NOTICE.format(
                        count=len(unknown),
                        shots=", ".join(_short(shot_id) for shot_id in unknown),
                    ),
                )
            )
        notices.extend(rejected)
        # A model that returned no sentence of its own must not leave the reply as a bare
        # separator followed by notices.
        message = result.message.strip() or EXPANSION_EMPTY_MESSAGE
        project.messages.append(assistant_reply(message, notices))
        return store.save(project, if_generation=generation)

    @app.post("/api/projects/{project_id}/assistant/fill", response_model=Project)
    async def assistant_fill(project_id: str, request: AssistantRequest) -> Project:
        """Fill in the selected Shots from one plain-language request. Assistant ProducerBot.

        The Director's language model with two tools. `fill_shots`' arguments are the shot taxonomy
        itself — `ShotMode`, `AssetRole`, `SingingState` — so a malformed answer is a validation
        error at the edge rather than a plausible string in the manifest. `expand_prompts` is how a
        conversational request reaches the H3 expansion specialist: ProducerBot is the surface and
        the specialists are in its box, so the specialist has no chat of its own. It costs one model
        call *per shot named*, runs after the fills so it expands the intent this turn wrote, and
        passes through `expand_shots` — every refusal a Director's own click meets, unchanged.

        Six properties are load-bearing, and each has a test that breaks if it stops holding:

        * **The selection is the scope.** `request.shot_ids` decides what the model is shown and
          what it may write to. A tool call naming anything else is refused and reported, including
          a real, unlocked Shot elsewhere in the plan. This is what stops tool-calling from widening
          what the assistant can act *on* while it widens what it can do.
        * **Every guard a Director's own click meets.** The lock and the render-provenance rules are
          `shot_write_refusal`, shared with expansion; the prompt gate is `batch.prompt_rejection`
          through `assistant_prompt_rejection`; the mode rules are `mode_specification_problems`;
          the library check is `dangling_citations`. Nothing here reimplements any of them.
        * **No GPU time, on every path.** Nothing in this route touches `comfy`, sets a `status`,
          queues a job, generates an image or promotes an asset. The Director's own description puts
          image generation *after* this, as their next act.
        * **All or nothing per Shot.** A Shot's answer is judged whole and applied whole. A refused
          prompt or an invented asset id discards that Shot's mode and citations with it, because
          a Shot carrying half of an answer looks filled in and is not.
        * **Nothing is persisted until every Shot has been judged.** There is one terminal
          `store.save`, and candidates are built first and committed second, so a failure part-way
          through leaves both the manifest and the in-memory project untouched rather than
          half-written.
        * **Every selected Shot is named in the reply.** Applied, locked, carrying provenance,
          refused, discarded, omitted or answered-for-and-empty: a Shot the Director explicitly
          picked and heard nothing about is the silence this feature is forbidden to have.

        Nothing infers `singing`. The model may *set* it, which is a visible act reported in the
        applied notice; a `None` from the tool leaves whatever the Shot already says, and no branch
        here derives it from a mode, a citation or a prompt.

        The empty-selection refusal, the re-read after the await, the 503/502 mapping and the single
        terminal save all follow `expand_shot_prompts`.
        """
        # Built from the pre-await snapshot, exactly as the chat and expansion prompts are.
        snapshot = get_project(project_id)
        held = {shot.id: shot for shot in snapshot.shots}
        # Deduplicated with order kept: a client that sends one id twice must not make the model
        # answer about it twice, and `dict.fromkeys` is the codebase's stable dedupe.
        requested = list(dict.fromkeys(request.shot_ids))
        selected = [held[shot_id] for shot_id in requested if shot_id in held]
        if not selected:
            raise HTTPException(status_code=422, detail=ASSISTANT_WITHOUT_SHOTS)
        # Refused *before* the call when nothing in the selection could be written to, on
        # EXPANSION_WITHOUT_SHOTS' argument: the model would spend the Director's seconds to be
        # told what this sentence already says. The wordings are the ones the reply would have
        # carried, so the refusal before the call and the notice after it agree.
        blocked: dict[str, list[str]] = {"locked": [], "rendered": []}
        for shot in selected:
            if reason := shot_write_refusal(shot):
                blocked[reason].append(shot_label(snapshot, shot))
        if len(blocked["locked"]) + len(blocked["rendered"]) == len(selected):
            reasons = " ".join(
                wording.format(shots=", ".join(names))
                for wording, names in (
                    (EXPANSION_LOCKED_NOTICE, blocked["locked"]),
                    (EXPANSION_RENDERED_NOTICE, blocked["rendered"]),
                )
                if names
            )
            raise HTTPException(
                status_code=422,
                detail=ASSISTANT_WITHOUT_WRITABLE_SHOTS.format(reasons=reasons),
            )
        try:
            # The requested ids verbatim, not the resolved Shots: `assistant_input` skips the ones
            # this project no longer has, and a test that asserts the route sent the builder's
            # output has to be asserting about a call the builder could have been given directly.
            turn = await director.assist(
                message=request.message,
                assistant_input=assistant_input(snapshot, shot_ids=requested),
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # Re-read after the await, for the reason `director_chat` documents, and here it is also
        # what makes the selection meaningful: a Shot deleted, locked or rendered while the model
        # was thinking is in this project and not in the snapshot the answer was written against.
        #
        # **And with the write generation, which matters more here than anywhere else in this
        # module.** This is the widest read-modify-write window in the application: the re-read
        # below is followed by an expansion sweep that spends one model call *per requested
        # shot*, and this project's own measurements put a single call at up to 300 seconds. A
        # whole-manifest save at the end of that would revert minutes of anything the Director
        # did meanwhile, silently, with this turn answering 200.
        #
        # The cost of refusing instead is stated plainly because it is real: a turn refused here
        # has already spent every one of those calls, and `SAVE_RACE_REFUSAL` tells the Director
        # nothing was saved. That is the trade this codebase has already made once — reverting
        # thirty-two prompts silently is the failure that produced `ShotListRequest.updated_at`
        # — and the better answer, re-applying the turn's own writes onto a fresh manifest the
        # way `record_submission` does, is a restructuring of this route rather than a token.
        project, generation = get_project_for_update(project_id)
        shots_by_id = {shot.id: shot for shot in project.shots}
        position = {shot.id: index for index, shot in enumerate(project.shots)}
        present = [shot_id for shot_id in requested if shot_id in shots_by_id]
        # Re-checked rather than assumed from the snapshot, exactly as expansion re-checks: saving
        # a reply about shots that no longer exist would leave the thread asserting a fill of
        # nothing.
        if not present:
            raise HTTPException(status_code=422, detail=ASSISTANT_WITHOUT_SHOTS)
        missing_targets = [shot_id for shot_id in requested if shot_id not in shots_by_id]
        labels = {shot_id: shot_label(project, shots_by_id[shot_id]) for shot_id in present}
        # Swept over the *selection* rather than over the model's answer, which is the difference
        # between this and expansion's equivalent lists. Expansion leaves a locked Shot the model
        # never mentioned unreported, because nothing was going to be written for it either way;
        # here the Director explicitly picked it, so "why did nothing happen to the shot I chose"
        # is a question the reply has to answer whether or not the model addressed it.
        locked: list[str] = []
        rendered: list[str] = []
        writable: list[str] = []
        for shot_id in present:
            reason = shot_write_refusal(shots_by_id[shot_id])
            (locked if reason == "locked" else rendered if reason == "rendered" else writable).append(
                shot_id
            )
        open_to_writing = set(writable)

        staged: list[tuple[int, Shot]] = []
        summaries: list[str] = []
        answered: set[str] = set()
        duplicated: list[str] = []
        out_of_scope: list[str] = []
        empty_fills: list[str] = []
        unknown_assets: list[MessageNotice] = []
        rejected: list[MessageNotice] = []
        specification: list[str] = []
        # The identity-sheet rule is populate's, applied here for the reason it is one function
        # in `models` rather than a branch in one route: this is the *other* writer of citations
        # from a model's answer, and it has the same defect — the assistant is offered the source
        # picture and the sheet promoted from it as two library rows, and a shot conditioned on
        # the single frame is using the weaker of the two. `substituted` collects the shots it
        # changed, because a substitution the reply does not mention is one the Director would
        # have to diff the manifest to find.
        sheets = identity_sheet_ids(project)
        substituted: list[str] = []
        for fill in turn.fills:
            if fill.shot_id not in open_to_writing:
                # A Shot the selection already reports on — locked, or carrying provenance — is not
                # reported a second time as out of scope: it *was* in scope, and the reply already
                # says in the Director's own vocabulary why nothing happened to it.
                if fill.shot_id in labels:
                    continue
                if fill.shot_id not in out_of_scope:
                    out_of_scope.append(fill.shot_id)
                continue
            # First answer wins, before any other check, on `expand_shot_prompts`' argument:
            # last-write-wins would let one Shot be reported as both refused and filled in.
            if fill.shot_id in answered:
                if fill.shot_id not in duplicated:
                    duplicated.append(fill.shot_id)
                continue
            answered.add(fill.shot_id)
            shot = shots_by_id[fill.shot_id]
            # `None` means leave it alone, never clear it. A model that names only a mode must not
            # thereby blank the prompt a Director wrote by hand, so the change set is built from
            # the keys that are actually present.
            changes: dict[str, Any] = {}
            redirected = False
            if fill.mode is not None:
                changes["mode"] = fill.mode
            # Set, never inferred: this is only reached because the tool call carried a value, and
            # the applied notice says so out loud. See `models.SingingState`.
            if fill.singing is not None:
                changes["singing"] = fill.singing
            if fill.citations is not None:
                asked = [
                    AssetCitation(**citation.model_dump()) for citation in fill.citations
                ]
                preferred = prefer_identity_sheets(asked, sheets)
                redirected = preferred != asked
                changes["citations"] = [
                    citation.model_dump() for citation in preferred
                ]
            if fill.prompt is not None:
                reason = assistant_prompt_rejection(fill.prompt)
                if reason:
                    # The whole answer for this Shot goes, not just its prompt. Applying the mode
                    # and the citations from an answer whose prompt was refused would leave a Shot
                    # that reads as filled in and cannot be rendered — and the refused text travels
                    # in `raw`, which `DIRECTOR_CONTEXT_EXCLUDE` keeps out of the next call.
                    rejected.append(
                        rejection_notice(
                            EXPANSION_REJECTED_NOTICE,
                            EXPANSION_REJECTED_EMPTY_NOTICE,
                            raw=fill.prompt,
                            shot=labels[fill.shot_id],
                            reason=reason,
                        )
                    )
                    continue
                changes["prompt"] = fill.prompt
            if not changes:
                empty_fills.append(fill.shot_id)
                continue
            # Validated as a whole Shot rather than assigned field by field, which is what makes
            # the citation/`asset_ids` reconciliation run and what turns anything the tool schema
            # somehow let through into an error here rather than into a stored manifest.
            candidate = Shot.model_validate({**shot.model_dump(), **changes})
            # Only the ids *this answer* introduced. A citation that was already dangling — an
            # asset deleted out from under a Shot yesterday — is the inspector's report to make,
            # and refusing today's answer for it would make an unrelated stale reference into a
            # permanent block on the Shot.
            already_missing = set(dangling_citations(project, shot))
            introduced = [
                asset_id
                for asset_id in dangling_citations(project, candidate)
                if asset_id not in already_missing
            ]
            if introduced:
                unknown_assets.append(
                    MessageNotice(
                        kind="refusal",
                        text=ASSISTANT_UNKNOWN_ASSET_NOTICE.format(
                            shot=labels[fill.shot_id],
                            count=len(introduced),
                            assets=", ".join(_short(asset_id) for asset_id in introduced),
                        ),
                    )
                )
                continue
            if problems := mode_specification_problems(candidate):
                # Reported, never a refusal: a mode with no adapter and a section laid out before
                # its images exist are both real planning work, and the refusal that matters
                # happens where GPU time would be spent.
                specification.append(f"{labels[fill.shot_id]}: {' '.join(problems)}")
            staged.append((position[fill.shot_id], candidate))
            # Recorded at the commit point, never at the substitution: a fill whose prompt was
            # refused is discarded whole, and reporting a substitution on a shot nothing was
            # written to would be reporting a change that did not happen.
            if redirected:
                substituted.append(fill.shot_id)
            summaries.append(f"{labels[fill.shot_id]}: {assistant_fill_summary(changes)}")
        # Nothing above this line has written to the project. What makes "a failure mid-sequence
        # leaves nothing half-applied" structural is the single terminal `store.save` below —
        # nothing is persisted until every Shot has been judged. Committing in one pass here is the
        # second half of it: the in-memory project a later reader sees is never half-written either.
        for index, candidate in staged:
            project.shots[index] = candidate
        omitted = [shot_id for shot_id in writable if shot_id not in answered]

        # The second tool, and it is a second *act* rather than a second field to assign: each shot
        # named here costs its own model call to the expansion specialist. It runs after the fills
        # are committed above, so a shot the model filled in and asked to expand in one turn is
        # expanded from the intent it has just written rather than from the one it replaced.
        #
        # The scope rule is `fill_shots`' own, and it has to be: a tool that could reach a shot the
        # Director did not select would widen what the assistant can act *on*, which is the guard
        # the whole selection-as-consent design exists to hold. The write refusal and the prompt
        # gate are not re-implemented here either — `expand_shots` applies both, in the order phase
        # one pinned — so `open_to_writing` is only about scope.
        wanted: list[str] = []
        for asked in turn.expansions:
            if asked.shot_id not in open_to_writing:
                # A shot the selection already reports on — locked, or carrying provenance — is not
                # reported again as out of scope, on the fill loop's argument exactly.
                if asked.shot_id in labels:
                    continue
                if asked.shot_id not in out_of_scope:
                    out_of_scope.append(asked.shot_id)
                continue
            # First mention wins. Expanding one shot twice in a turn would spend two model calls to
            # keep the second answer, which is a coin toss the Director is paying for.
            if asked.shot_id not in wanted:
                wanted.append(asked.shot_id)
        expansions: list[ShotExpansionOutcome] = []
        if wanted:
            # Rebuilt after the staged fills landed: the map above holds the Shot objects those
            # candidates replaced, so a payload built from it would describe the pre-fill shot.
            current = {shot.id: shot for shot in project.shots}
            try:
                expansions = await expand_shots(
                    project, [current[shot_id] for shot_id in wanted], director=director
                )
            except DirectorUnavailable as error:
                # Reported per shot rather than raised, unlike the sweep route. `director.assist`
                # has already answered, so this is all but unreachable — and raising here would
                # throw away a whole turn of good fills over the expansion half of it.
                expansions = [
                    ShotExpansionOutcome(shot_id, "failed", detail=str(error))
                    for shot_id in wanted
                ]
            expansions = apply_expansions(project, expansions)

        notices: list[MessageNotice] = []
        if staged:
            notices.append(
                MessageNotice(
                    kind="change",
                    text=ASSISTANT_APPLIED_NOTICE.format(
                        count=len(staged), details="\n".join(summaries)
                    ),
                )
            )
        # The lock and the provenance wordings are `expand_shot_prompts`' own, reused rather than
        # reworded. The frozen matrix asks for a refusal "in the same words a Director's click
        # gets", and these are the words every other automated write to a Shot already uses —
        # a second wording for one rule is how the two start describing different rules.
        for reported, wording, kind in (
            (locked, EXPANSION_LOCKED_NOTICE, "refusal"),
            (rendered, EXPANSION_RENDERED_NOTICE, "refusal"),
            (missing_targets, ASSISTANT_MISSING_TARGET_NOTICE, "refusal"),
        ):
            if reported:
                notices.append(
                    MessageNotice(
                        kind=kind,
                        text=wording.format(
                            shots=", ".join(
                                labels.get(shot_id, _short(shot_id)) for shot_id in reported
                            )
                        ),
                    )
                )
        # Beside the applied notice, because it is part of what was written rather than a refusal:
        # these shots were filled in, and the citations they got are not the ids the model named.
        if substituted:
            notices.append(
                MessageNotice(
                    kind="change",
                    text=ASSISTANT_IDENTITY_SHEET_NOTICE.format(
                        shots=", ".join(labels[shot_id] for shot_id in substituted)
                    ),
                )
            )
        notices.extend(unknown_assets)
        notices.extend(rejected)
        if out_of_scope:
            notices.append(
                MessageNotice(
                    kind="refusal",
                    text=ASSISTANT_OUT_OF_SCOPE_NOTICE.format(
                        count=len(out_of_scope),
                        shots=", ".join(_short(shot_id) for shot_id in out_of_scope),
                    ),
                )
            )
        if turn.malformed:
            notices.append(
                rejection_notice(
                    ASSISTANT_MALFORMED_NOTICE,
                    ASSISTANT_MALFORMED_EMPTY_NOTICE,
                    raw="\n".join(turn.malformed),
                    count=len(turn.malformed),
                )
            )
        for reported, wording in (
            (omitted, ASSISTANT_OMITTED_NOTICE),
            (empty_fills, ASSISTANT_EMPTY_FILL_NOTICE),
            (duplicated, ASSISTANT_DUPLICATE_NOTICE),
        ):
            if reported:
                notices.append(
                    MessageNotice(
                        kind="flag",
                        text=wording.format(
                            shots=", ".join(labels[shot_id] for shot_id in reported)
                        ),
                    )
                )
        if specification:
            notices.append(
                MessageNotice(
                    kind="flag",
                    text=ASSISTANT_SPECIFICATION_NOTICE.format(details="\n".join(specification)),
                )
            )
        # The expansion half of the turn, as one block after the fill's report. Its own ordering is
        # `expansion_sweep_notices`' — what was written, then what was refused, then what is worth
        # a look — and it reads after the fill because that is the order the two acts happened in.
        notices.extend(expansion_sweep_notices(expansions, labels))
        # Said only when the model produced nothing at all to act on. A turn that called a tool
        # and had every call refused is a different failure, and every one of those refusals is
        # already its own sentence above. `expansions` counts: a turn that only asked for
        # expansions called a tool, and telling it that it answered in prose would be false.
        if not turn.fills and not turn.expansions and not turn.malformed:
            notices.append(MessageNotice(kind="flag", text=ASSISTANT_WITHOUT_TOOL_CALL_NOTICE))
        message = turn.message.strip() or ASSISTANT_EMPTY_MESSAGE
        # The user's own turn is recorded, unlike expansion's — this one *was* a question, and the
        # thread is the audit trail for what the Director asked as well as for what was written.
        project.messages.append(TreatmentMessage(role="user", content=request.message))
        project.messages.append(assistant_reply(message, notices))
        # The other writer of citations, and it writes them onto shots that may already carry an
        # expansion. After `apply_expansions` above rather than before it, so a shot this turn both
        # re-cited and re-expanded keeps the expansion the model just wrote — that one is already
        # against the new citations and its recorded map says so, and this pass finds it fresh.
        refresh_reference_maps(project)
        return store.save(project, if_generation=generation)
