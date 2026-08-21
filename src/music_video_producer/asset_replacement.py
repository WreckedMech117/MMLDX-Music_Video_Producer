"""Re-point every citation of one library Asset at another. Pure, I/O-free, writes nothing.

The Director's own report (2026-08-20): *"I tried to remove HarderFaster image (Lucy) so it just
had the multiview but since its tied to shots it wouldnt let me, its fine that it caught that, but
a nice Replace With/Cancel option set would be nice."* `DELETE /assets/{id}` refuses a cited Asset
by name and that refusal is correct and stays; what was missing is the act the refusal implies —
move the citations, then delete.

**This module decides; it does not write.** `app.replace_asset_citations` reports the plan, and
only a second call carrying `confirm_apply` commits it, which is `populate`'s `confirm_replace`
shape enforced on the server exactly as `snap_cut_plan`'s report/apply pair is. Nothing here
renders, arms, queues or approves: the only fields a candidate Shot differs in are `citations`,
`reference_labels` and the `asset_ids` projection the model rebuilds from the first.

Lives in its own module rather than in `models` because it is a *plan over a whole project*, the
same class of thing `timeline.snap_cut_plan` is, and `models` holds the taxonomy those plans are
written against. It imports `models` and `timeline` and nothing else, so it stays a leaf: the
per-kind ceilings (`workflows.H3_REFERENCE_LIMITS`) arrive as the `limits` argument, exactly as
`models.with_default_setting` takes `picture_limit`, because `workflows` imports `timeline` and
the dependency may not run back the other way.

Three outcomes per shot, and they are the Director's own three:

* **swap** — the shot cites the replaced Asset and does not cite the replacement in that role.
  The citation is re-pointed in place: `role` and `order` are carried across untouched, because a
  citation is one slot and a replacement is a new occupant for it, not a new slot.
* **already have** — the shot cites both, and every citation of the replaced Asset has one of the
  replacement in the same role. The Director named this case
  themselves ("already in N shots ... which would simply remove the reference its trying to
  replace and leave its already present reference"), and it is live in their project between the
  HarderFaster source and the multiview promoted from it. Purely subtractive: the old citation
  goes, the standing one is not touched.
* **skipped** — reported by name, never guessed at. See `REPLACE_MIXED_ROLES` and
  `REPLACE_OVER_SLOT_LIMIT` below for the two this module decides; the protections (locked, and a
  render executing right now) are decided by the route and arrive in `protected`.

**A shot that has already rendered is replaced, and told about.** This module shipped on
2026-08-21 skipping every shot `app.shot_write_refusal` refused, which meant a rendered or approved
shot could not have its references changed at all. The Director overruled it the same day: *"So
even with takes we do want the asset for the shot replaceable, that way a re-render would use the
updated asset without losing previous takes. This helps facilitate experimentation."* The reasoning
holds and is worth stating, because it is the general rule for citations: **replacing a citation
does not touch the take.** The rendered file is still on disk, `latest_output` still names it, the
takes strip still lists it and `RenderJob.output_files` is unchanged — a citation describes what a
*future* render would use. Blocking the swap protected nothing and forced a choice between keeping
an old take and trying a better reference, which is the experimentation loop this application
exists for. Approved shots are the same case: an approval is a judgement about a take that still
exists untouched, and `approved_output`/`approved_start`/`approved_duration` are not written here
at all — citations are not the window, so AD-13's staleness comparison and assembly are unaffected.

What survives is the *report*: `provenance` marks those shots, because the consequence is real and
permanent. Nothing in this application records which assets produced an existing take, so after the
replacement a shot's take and its citations simply disagree, with no way to recover what the take
was actually conditioned on. Reported before the confirm, never blocked.

**Why `mode_specification_problems` is not re-checked here.** A swap changes no role's count at
all, and a merge only ever lowers the count of a role the shot already held *twice* — the removed
citation and the standing one share a role, by the definition of a merge. So no minimum can go
from satisfied to unsatisfied and no maximum from respected to exceeded by anything this module
does, and a branch for it would be unreachable code asserting a rule it cannot reach. The
per-kind *slot* ceilings are a different matter and are checked, because a replacement of another
`kind` moves a citation between H3's picture, video and audio series.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .models import (
    Asset,
    AssetCitation,
    Project,
    Shot,
    reference_slot_totals,
    shot_label,
)
from .timeline import ordered_shots

#: A shot citing both Assets in roles that do not line up. Skipped, and this is the deliberate
#: refusal rather than the plausible guess.
#:
#: The case: the shot cites the replaced Asset as a plain `reference` *and* the replacement as a
#: `first` keyframe. Neither available answer is knowable.
#:
#: * dropping the reference — the merge rule — leaves the shot with a keyframe and no plain
#:   reference at all, which silently changes what it renders;
#: * re-pointing it — the swap rule — leaves the shot citing one Asset twice, in two roles, so the
#:   render is conditioned on the same picture in two of H3's nine slots. That is the waste
#:   `models.prefer_identity_sheets` refuses to create ("the same subject argued twice rather than
#:   a subject and its context"), and it is not something the counts in the report would show.
#:
#: So the shot is named and left exactly as it is, and the Director resolves it in the inspector
#: where both citations are visible together. **A merge is all-or-nothing per shot**: it happens
#: only when every citation of the replaced Asset has a standing citation of the replacement in
#: the same role, which is the only shape where the change is purely subtractive and unambiguous.
#:
#: Unreachable for a shot whose citations of the two Assets hold the same roles, which is every
#: collision in the live project: all eight cite both in `reference`.
REPLACE_MIXED_ROLES = (
    "{shot} cites {replaced} as {replaced_roles} and already cites {replacement} as "
    "{replacement_roles}. Those do not line up, so replacing one would either drop a role this "
    "shot still needs or leave the same asset cited twice over — decide it in the shot inspector, "
    "where both citations are listed."
)

#: A replacement that would not fit in the slots H3 wires. Only ever reachable through a change of
#: kind: a swap keeps the number of citations exactly, so the count can only move when a citation
#: crosses between the picture, video and audio series (`models.citation_slot_kind`).
#:
#: Counted through `reference_slot_totals`, which is the numbering's own count and includes the
#: master song's audio slot, so the ceiling checked here is the one `workflows` would refuse
#: against later. A shot already over a ceiling before this replacement is **not** skipped for it —
#: only an overflow this replacement introduces counts, which is `assistant_fill`'s
#: already-missing/introduced rule applied to slots instead of to dangling ids.
REPLACE_OVER_SLOT_LIMIT = (
    "{shot} would then wire {count} {kind} references and H3 accepts at most {limit}. "
    "{replacement} is a {kind} where {replaced} was one of this shot's pictures, so the "
    "replacement does not fit. Remove a reference from this shot first."
)


@dataclass(frozen=True, slots=True)
class ReplacementChange:
    """One shot the replacement would change, and what it would do to it.

    `roles` is every role the replaced Asset is cited in on this shot, in citation order, because
    "role and order are carried across" is the guarantee the Director has to be able to check
    without opening the manifest. `carried_label` is the `reference_labels` entry that moves — `""`
    when the shot has none for either Asset, or when the standing citation's own label wins.

    `provenance` is `"approved"`, `"rendered"` or `""`: whether this shot already holds a take that
    was produced against the *old* asset. It changes nothing about what happens to the shot — see
    the module docstring — and exists so the count is on screen before the confirm, because nothing
    in this application records which assets produced an existing take.
    """

    shot_id: str
    label: str
    roles: tuple[str, ...]
    carried_label: str
    provenance: str = ""


@dataclass(frozen=True, slots=True)
class ReplacementSkip:
    """One shot left exactly as it is, and the sentence saying why."""

    shot_id: str
    label: str
    reason: str


@dataclass(slots=True)
class ReplacementPlan:
    """What a replacement would do to a whole project, and the rewritten Shots to commit.

    `candidates` is keyed by shot id and holds fully validated `Shot`s — built through
    `Shot.model_validate`, which is what makes the `asset_ids` projection reconcile from the new
    citations rather than being rebuilt by hand here. The route commits them by position in one
    pass after the report is complete; nothing in this module touches `project`.
    """

    swaps: list[ReplacementChange] = field(default_factory=list)
    merges: list[ReplacementChange] = field(default_factory=list)
    skips: list[ReplacementSkip] = field(default_factory=list)
    #: Every rewrite in walk order, swaps and merges interleaved. The same objects the two buckets
    #: hold, not copies — this is the *order* they happened in, which is the song's.
    changes: list[ReplacementChange] = field(default_factory=list)
    candidates: dict[str, Shot] = field(default_factory=dict)

    @property
    def cited(self) -> int:
        """How many shots cite the replaced Asset at all — the three buckets together."""
        return len(self.swaps) + len(self.merges) + len(self.skips)

    @property
    def writes(self) -> int:
        """How many shots this plan would actually rewrite."""
        return len(self.candidates)

    def with_provenance(self, kind: str) -> list[ReplacementChange]:
        """The shots this plan rewrites that already hold a take, in song order.

        Read off `changes` rather than counted a second time, so the number in the report and the
        rows beside it can never disagree. `changes` is every rewrite in walk order — both buckets
        interleaved — because a rendered shot that also cites the replacement is an "already have"
        and is exactly as affected, and a report that listed all the swaps before all the merges
        would name the shots in an order the timeline does not have.
        """
        return [change for change in self.changes if change.provenance == kind]


def asset_replacement_plan(
    project: Project,
    *,
    replaced: Asset,
    replacement: Asset,
    protected: Mapping[str, str],
    provenance: Mapping[str, str],
    limits: Mapping[str, int],
) -> ReplacementPlan:
    """Every shot citing `replaced`, bucketed, with the rewritten Shot for each one that changes.

    Walked in `ordered_shots` order — song order, which is the order the Director watches the
    video in and reads a report in. Shots citing neither Asset are not in the plan at all: they
    are untouched, and a report row saying so about eight of thirty shots is noise.

    `protected` maps shot id to the sentence explaining why that shot may not be written to, and
    holds **only** the two protections that survive the Director's ruling: a lock, which is an
    explicit hands-off only they may clear, and a render executing right now, which is the one
    genuine correctness block — rewriting a citation mid-flight would leave the job record
    describing a render that never happened. It is data rather than a rule applied here, so the
    wordings stay in the route beside every other refusal instead of becoming a second opinion that
    can drift. A protected shot is skipped **before** anything about its citations is examined, so
    no protected shot can be reported as anything but skipped.

    `provenance` maps shot id to `"approved"`, `"rendered"` or nothing. It changes no decision —
    every shot in it is rewritten like any other — and is copied onto the change rows so the report
    can carry the count. See the module docstring for why it is a note and not a refusal.

    `limits` is `workflows.H3_REFERENCE_LIMITS` — `{"picture": 9, "video": 3, "audio": 3}` — and a
    kind absent from it is unbounded.
    """
    plan = ReplacementPlan()
    for shot in ordered_shots(project):
        cited = [
            citation for citation in shot.citations if citation.asset_id == replaced.id
        ]
        if not cited:
            continue
        label = shot_label(project, shot)
        if reason := protected.get(shot.id, ""):
            plan.skips.append(
                ReplacementSkip(shot_id=shot.id, label=label, reason=reason)
            )
            continue
        roles = tuple(citation.role for citation in cited)
        standing = {
            citation.role
            for citation in shot.citations
            if citation.asset_id == replacement.id
        }
        # A shot the replacement would have to *both* re-point and drop citations on, or would
        # have to give a second role to an asset it already cites. Either is a guess; see
        # `REPLACE_MIXED_ROLES`. A merge is therefore only ever the whole shot: every citation of
        # the replaced Asset has a standing citation of the replacement in the same role.
        swapped = [citation for citation in cited if citation.role not in standing]
        if swapped and standing:
            plan.skips.append(
                ReplacementSkip(
                    shot_id=shot.id,
                    label=label,
                    reason=REPLACE_MIXED_ROLES.format(
                        shot=label,
                        replaced=replaced.name,
                        replacement=replacement.name,
                        replaced_roles=", ".join(dict.fromkeys(roles)),
                        replacement_roles=", ".join(sorted(standing)),
                    ),
                )
            )
            continue
        citations: list[AssetCitation] = []
        for citation in shot.citations:
            if citation.asset_id != replaced.id:
                citations.append(citation)
            elif citation.role not in standing:
                # Re-pointed in place. `role` and `order` are the slot this citation occupies and
                # the position it holds within that role, and neither is a fact about *which*
                # asset sits there — carrying anything less would move the picture and silently
                # renumber the prompt tags written against it.
                citations.append(
                    AssetCitation(
                        asset_id=replacement.id, role=citation.role, order=citation.order
                    )
                )
            # else: a merge — dropped, and the standing citation of the replacement is not
            # touched. Its `order` is left as it is; `order` sorts within a role and is not a
            # dense index, so the gap a dropped citation leaves changes no ordering.
        labels = dict(shot.reference_labels)
        # **The label that travels is the surviving citation's own.** One rule, read twice: on a
        # swap the survivor *is* the replaced Asset's citation, re-pointed, so its label moves onto
        # the new id and wins over any entry already stored there (the replacement was not cited by
        # this shot, so such an entry is an orphan from a citation that no longer exists). On a
        # merge the survivor is the standing citation of the replacement, so *its* label wins and
        # the removed citation's is dropped with it — the merge is meant to be purely subtractive,
        # and a removal that also renamed the surviving reference would change what an
        # already-authored `h3_prompt` calls that picture in the reference map beneath it.
        #
        # The one place a removed label is carried: a merge onto a survivor that has no label at
        # all. Nothing is overwritten, nothing is invented, and the Director's word for the subject
        # ("Lucy") survives the promotion to the multiview instead of the shot silently falling
        # back to the sheet's internal name.
        #
        # Entries for every other asset are left exactly as they are, including an orphan under the
        # replaced Asset on a shot that does not cite it — that shot is not in this plan.
        moving = labels.pop(replaced.id, "")
        carried = ""
        if moving and (swapped or not labels.get(replacement.id)):
            labels[replacement.id] = moving
            carried = moving
        # Validated as a whole Shot rather than assigned field by field, `assistant_fill`'s rule
        # and for its reason: this is what runs `Shot._reconcile_citations`, so `asset_ids` is
        # rebuilt as the projection of the new reference-role citations instead of being kept in
        # sync by a second hand here.
        candidate = Shot.model_validate(
            {
                **shot.model_dump(),
                "citations": [citation.model_dump() for citation in citations],
                "reference_labels": labels,
            }
        )
        before = reference_slot_totals(project, shot)
        overflow = [
            (kind, total, limits[kind])
            for kind, total in reference_slot_totals(project, candidate).items()
            if kind in limits and total > limits[kind] and total > before.get(kind, 0)
        ]
        if overflow:
            kind, total, limit = overflow[0]
            plan.skips.append(
                ReplacementSkip(
                    shot_id=shot.id,
                    label=label,
                    reason=REPLACE_OVER_SLOT_LIMIT.format(
                        shot=label,
                        count=total,
                        kind=kind,
                        limit=limit,
                        replaced=replaced.name,
                        replacement=replacement.name,
                    ),
                )
            )
            continue
        change = ReplacementChange(
            shot_id=shot.id,
            label=label,
            roles=roles,
            carried_label=carried,
            provenance=provenance.get(shot.id, ""),
        )
        (plan.swaps if swapped else plan.merges).append(change)
        plan.changes.append(change)
        plan.candidates[shot.id] = candidate
    return plan
