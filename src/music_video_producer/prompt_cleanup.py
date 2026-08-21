"""Take the asset labels back out of a plan's prose. Pure, I/O-free, writes nothing.

The Director's report on a live 33-shot plan (2026-08-21): the shot prompts read like an
inventory rather than like direction.

    "Extreme close up of Crimson Lips Close-up while HarderFaster performs."
    "Blue Haze Atmosphere surrounding the Dusk Warehouse Bed."

The first says "close up of ... Close-up". 24 of the 33 prompts carry a non-character asset's
display name. The cause is not the model being careless: until `director.PlannedShot.assets`
existed, **naming the asset in the prose was the only way to cite it** — `populate`'s citation
step scanned the prompt for display names, so a shot that wanted a picture had to write that
picture's label into the sentence a renderer would later read. The prose was the attachment
mechanism, and the wording is what it cost.

`PlannedShot.assets` fixes that for plans made from now on. This module fixes the plan the
Director already has, and it has one dominating constraint.

**The windows are sacred.** The Director's words: *"I have done some timeline touch ups and am
generally happy for now."* Those 33 windows are hand-placed against musical timing and include
several deliberate micro-cuts (0.5 s, 1.75 s, 1.83 s, 1.88 s, 2.08 s, 2.67 s). Re-populating
would destroy that work irrecoverably, so nothing here proposes a window, an order or a count:
`window_fingerprint` exists so the route can *prove* the geometry it saved is the geometry it
loaded, rather than promising it in a comment.

**Only `Shot.prompt` changes, and the citations are what make that safe.** The asset ids are
already stored on each shot, in roles, with orders — so the prose no longer has to carry the
names for the render to find the pictures. Removing a label from a sentence removes nothing
from the shot. That is the whole argument for this being a prose edit and not a citation edit,
and it is why `app.clean_shot_prompts` asserts the citation set byte-identical across the
write rather than merely leaving it alone.

**This module decides; it does not write, and it does not call a model.** The route reports
the plan, and only a second call carrying `confirm_apply` commits it — `populate`'s
`confirm_replace` shape, enforced on the server exactly as `snap_cut_plan` and
`asset_replacement_plan` enforce it. Nothing here renders, arms, queues or approves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import NAME_SCAN_MIN_LENGTH, Asset, AssetKind, Project, Shot
from .timeline import ordered_shots, song_section

#: Asset kinds whose display name is allowed to stay in the prose.
#:
#: Exactly one, and the Director drew the line themselves: the measurement is "24 of 30 prompts
#: echo a **non-character** asset name", and the sentence quoted as the *bad* example —
#: "Extreme close up of Crimson Lips Close-up while HarderFaster performs" — is bad in its first
#: half and correct in its second. A character asset's display name is the performer's name. It
#: is what the Director says out loud, what the treatment is written in, and what tells a
#: renderer who is in frame; strip it and "she sings into the lens" has no subject, which is
#: worse prose than the echo. Every other kind's name is a library label for a picture of a
#: thing — a prop, a place, a look — and writing it is what makes a shot list read like stock.
#:
#: The gap this leaves, stated rather than hidden: a *character* asset whose display name
#: carries a promotion suffix ("HarderFaster · multiview") is exempted along with the plain
#: name, so a prompt that spelled the suffix would keep it. Nothing in this codebase can create
#: that prompt any more — `models.citable_assets` withholds a promoted sheet's name from the
#: model, and `PlannedShot.assets` means no name needs writing at all — and narrowing the
#: exemption to "the part before the separator" would be a string heuristic standing between
#: the Director and their performer's name. Reported here so the next reader knows it is a
#: decision and not an oversight.
ECHO_EXEMPT_KINDS: frozenset[AssetKind] = frozenset({"character"})

#: What a rewrite is refused for, one sentence each, because a skip without a why is the report
#: step doing nothing (`SECTION_LOOK_SKIP_*`'s argument, and `SnapCutSkip`'s).
CLEANUP_REJECT_EMPTY = "the model returned an empty prompt"
CLEANUP_REJECT_JSON = (
    "the model returned JSON rather than prose, and a prompt is the text a renderer reads"
)
CLEANUP_REJECT_STILL_ECHOES = (
    "the rewrite still names {labels}, which is the one thing it was asked to remove"
)
CLEANUP_REJECT_GUTTED = (
    "the rewrite is {after} characters against the original's {before} — that is a deletion "
    "rather than a rephrasing, and the shot would lose its direction along with the label"
)

#: How much shorter than the original a rewrite may be before it reads as a deletion.
#:
#: Half, with a floor, and both halves of that are needed. The ratio catches the common local
#: failure — a model that "removes the label" by returning the three words either side of it —
#: while the floor keeps a genuinely short original ("Glamour shot of HarderFaster posing on
#: the bed.") from being measured against a threshold smaller than a sentence. Deliberately
#: generous: this is a guard against gutting, not a style checker, and a rewrite the Director
#: dislikes is one they can see in the report and decline, where a rewrite that silently ate
#: the direction is one they would find later in a render.
CLEANUP_MIN_RATIO = 2
CLEANUP_MIN_CHARACTERS = 20


def echoed_labels(prompt: str, assets: Sequence[Asset]) -> list[str]:
    """The asset display names this prose spells out **verbatim**, in library order.

    Case-**sensitive**, unlike `models.assets_for_proposal`'s fallback scan, and the difference
    is the definition of the defect rather than an inconsistency. What the Director objected to
    is a *label* sitting in creative prose: "Flickering light glints off the Silver Spiked
    Choker" reads as inventory because the capitals make it a proper noun naming a library row.
    "Flickering light glints off the silver spiked choker" is the same words and is simply a
    description of what is in shot — and it is one of the best available fixes. A
    case-insensitive rule would report that sentence as still broken and would forbid the
    cleanest rewrite there is.

    The consequence, stated: a prompt that already spells a name in a different case still
    *cites* that asset — the scan that built the citation is case-insensitive — and is not
    reported here as an echo. That is the intended pair. The citation is already stored on the
    shot and is not this pass's to change; only the reading of the sentence is.

    `NAME_SCAN_MIN_LENGTH` is shared with that scan and for its reason: a three-character name
    matches inside ordinary words, and a substring test with no floor would report "Bed" inside
    "Bedroom".

    `ECHO_EXEMPT_KINDS` is applied here rather than by the caller so that "what counts as an
    echo" has one definition. An empty result means this prompt is already clean and the route
    leaves the shot completely alone.
    """
    return [
        asset.name
        for asset in assets
        if asset.kind not in ECHO_EXEMPT_KINDS
        and len(asset.name) >= NAME_SCAN_MIN_LENGTH
        and asset.name in prompt
    ]


def rewrite_rejection(candidate: str, *, original: str, labels: Sequence[str]) -> str:
    """Why a rewritten prompt must not be written onto a Shot, or `""` when it may.

    `expansion_rejection`'s shape and its rule — check at the route, report to the Director,
    never swallow — with the two checks that are specific to this pass.

    The label check is the point of the whole feature and is therefore not advisory: a rewrite
    that still contains the name it was asked to remove is a call that did nothing, and writing
    it would spend the Director's review on a no-op. Two things bound it, both deliberate.

    It is checked against the labels **this shot** actually echoed, not against the whole
    library, so a rewrite that legitimately introduces a different asset's word ("a bed") is
    not caught by a name it never had to remove.

    And it is **case-sensitive**, `echoed_labels`' rule and for `echoed_labels`' reason. This
    was measured against the Director's own plan before it shipped: a case-insensitive check
    rejected 6 of the 24 rewrites, and every one of them was *correct* — "Flickering light
    glints off the silver spiked choker" and "HarderFaster sings into the vintage chrome
    microphone" both still contain their label case-insensitively, and both are exactly the
    prose this pass exists to produce. Lowercasing a proper-noun label into an ordinary noun
    phrase is a fix, not a failure to fix.

    The length check is the gutting guard; see `CLEANUP_MIN_RATIO`. Order matters between the
    two only in what the Director reads first, and "it still says the label" is the more useful
    sentence, so it comes first.
    """
    text = candidate.strip()
    if not text:
        return CLEANUP_REJECT_EMPTY
    # A prompt is prose. This is `document_rejection`'s JSON-as-prose concern in the one form
    # it can take here, and it is cheap insurance on a model family this project has measured
    # returning its own scaffolding.
    if text[0] in "{[":
        return CLEANUP_REJECT_JSON
    remaining = [label for label in labels if label in text]
    if remaining:
        return CLEANUP_REJECT_STILL_ECHOES.format(labels=", ".join(remaining))
    floor = max(CLEANUP_MIN_CHARACTERS, len(original.strip()) // CLEANUP_MIN_RATIO)
    if len(text) < floor:
        return CLEANUP_REJECT_GUTTED.format(after=len(text), before=len(original.strip()))
    return ""


def window_fingerprint(project: Project) -> tuple[tuple[str, float, float], ...]:
    """Every shot's identity and window, in manifest order. The thing that must not change.

    Manifest order rather than song order, deliberately: the Director's plan is not stored in
    song order — the last four of their 33 shots sit at the end of the file and play at 0 s,
    62.75 s, 64.58 s and 107.5 s — so a fingerprint taken in sorted order would be blind to a
    reordering of the list itself, and `shot_label` numbers the timeline by manifest position,
    so a reorder renames every clip on screen even when no window moved.

    Compared as a whole, so it also pins the **count**: a shot added or removed changes the
    tuple's length. `start` and `duration` are compared as the floats they are stored as, not
    rounded, because the guard exists to catch a write that was not supposed to happen at all
    and a tolerance is a hole in it.

    A function rather than an inline comprehension because it is asserted in three places — the
    route's own guard before it saves, the same guard against the saved manifest, and the tests
    — and three spellings of "the geometry did not move" is how two of them start meaning
    something slightly different.
    """
    return tuple((shot.id, shot.start, shot.duration) for shot in project.shots)


def citation_fingerprint(shot: Shot) -> tuple[tuple[str, str, int], ...]:
    """One shot's citations as comparable data: asset id, role and order, in stored sequence.

    The other half of what this pass may not touch. Citations are why removing a label from
    the prose is safe — the ids are already on the shot, so the sentence does not have to
    carry them — and a cleanup that quietly dropped one would remove the very thing that makes
    it safe. Sequence-sensitive on purpose: `order` sorts within a role and the stored order is
    what `citations_in_prompt_order` walks, so a reshuffle is a change even when the set is the
    same.
    """
    return tuple(
        (citation.asset_id, citation.role, citation.order) for citation in shot.citations
    )


#: The job description. One whole-plan call, riding `director.expand`'s wire and
#: `ShotExpansion`'s contract — "revised prompts addressed by shot id" is exactly this pass's
#: output shape too, which is `dp_prompt.DP_SYSTEM_PROMPT`'s argument reused rather than a
#: second transport invented.
#:
#: Whole-plan rather than per-shot for a reason this pass has of its own: the same label appears
#: in many shots ("Dusk Warehouse Bed" in six of the Director's), and one context is the only
#: place a model can avoid replacing it with the identical phrase six times over.
#:
#: The rules are stated as prohibitions with examples because this model family follows examples
#: and ignores abstractions — measured five ways in this project. The strongest one is negative:
#: **change nothing but the label**, because every other word in these prompts is the Director's
#: own hand-reviewed direction and this pass has no mandate to improve it.
PROMPT_CLEANUP_SYSTEM_PROMPT = """You are a script editor inside a local music-video production editor.

The shot prompts in this plan were written by a tool that could only attach a picture to a
shot by spelling that picture's library label inside the shot's prose. The pictures are now
attached properly, as data, so the labels are dead weight — and they read like an inventory
instead of like direction. Your one job is to take them out.

The input is a JSON object with these keys:
- treatment, style_bible: the creative work these prompts belong to. Read them for the
  vocabulary this video is written in. Do not quote them.
- shots: every shot whose prompt still names a label, in song order.

Each entry in shots has:
- shot_id: the shot's identity. Copy it verbatim into your answer; it is how your rewrite is
  matched to a shot. A wrong or invented id is discarded.
- prompt: the prompt as it stands today.
- labels: the exact library labels that must not appear in your rewrite.
- section: when the song's structure is marked, this shot's section and its shared look.

For each shot, return the SAME prompt with the labels replaced by ordinary description.

Rules, in order of importance:
1. Change nothing except the label and the words needed to make the sentence read. The
   camera, the framing, the action, the mood, the subject and the sentence's shape are the
   director's own and are not yours to improve. This is an edit, not a rewrite.
2. None of the listed labels may appear in your answer as written. Lowercasing one into an
   ordinary noun phrase counts as fixing it — "the Silver Spiked Choker" becomes "the silver
   spiked choker" — because what makes it read like a catalogue entry is that it is
   capitalised as a name.
3. Replace each label with what the thing actually is, in plain words. "Crimson Lips
   Close-up" is smeared red lips. "Dusk Warehouse Bed" is a bed in a dark warehouse — or
   just "the bed" when the shot has already established it. "Blue Haze Atmosphere" is drifting
   blue haze. "Silver Spiked Choker" is a silver spiked choker. "Vintage Chrome Mic" is a
   vintage chrome microphone. "Gritty Warehouse Floor" is a cracked concrete floor.
4. Fix what the label made ungrammatical. "Extreme close up of Crimson Lips Close-up" must not
   become "Extreme close up of close-up"; it is "Extreme close up of smeared crimson lips".
5. Some labels already read as ordinary English in place. "HarderFaster arches sensually on
   the Dusk Warehouse Bed" is a good sentence with one label in it: the fix is "on the dusk-lit
   warehouse bed", not a new shot. Leave everything else exactly as it is.
6. Performer names are NOT labels and must stay. If the prompt says HarderFaster sings, your
   answer says HarderFaster sings — dropping the name leaves the shot with no subject, and
   dropping the singing produces a beautiful shot of a closed mouth.
7. Never invent an asset, a location or an action the prompt did not already have. Never quote
   or invent song lyrics.

Return one revised prompt per shot, with shot_id copied verbatim. Answer for every shot you
are given, and for no others."""


def prompt_cleanup_input(
    project: Project, echoes: Mapping[str, Sequence[str]]
) -> dict[str, Any]:
    """The whole cleanup ask, label-relevant fields only. Pure and I/O-free.

    `echoes` maps shot id to the labels that shot must lose, and it is also the **selection**:
    only shots in it are sent. A shot whose prose is already clean is not a question the model
    should be asked, and including it would invite a rewrite of a prompt nobody complained
    about — which on a hand-reviewed plan is the expensive kind of helpfulness.

    Walked in `ordered_shots` order, so the model reads the plan the way the video plays and
    can vary its replacements across neighbouring shots rather than reaching for the same
    phrase every time a label repeats.

    Deliberately trimmed, `dp_input`'s rule: no citations (the pass does not re-cast), no
    windows (it may not retime and does not need to know), no render state, no asset roster.
    The labels a shot must lose ride on that shot, and the library is not otherwise described —
    a roster here would be a fresh list of names to echo, which is the defect in a new room.
    """
    shots: list[dict[str, Any]] = []
    for shot in ordered_shots(project):
        labels = list(echoes.get(shot.id, ()))
        if not labels:
            continue
        entry: dict[str, Any] = {
            "shot_id": shot.id,
            "prompt": shot.prompt,
            "labels": labels,
        }
        section = song_section(project, shot)
        if section is not None:
            entry["section"] = {"label": section.label, "prompt": section.prompt}
        shots.append(entry)
    return {
        "treatment": project.treatment,
        "style_bible": project.style_bible,
        "shots": shots,
    }
