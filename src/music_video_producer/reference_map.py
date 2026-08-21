"""The reference map an H3 prompt is conditioned on, and whether a stored expansion still fits it.

This module exists for one reason: **"is this shot's stored expansion stale" has to have exactly
one implementation**, and two of its readers sit on opposite sides of the import graph. The submit
route (`app.generate_h3`) refuses a stale map, and the pre-flight (`batch.readiness_report`) has to
report the same verdict *before* the Director confirms the GPU cost — and `app` imports `batch`, so
the answer could not live in `app`. A second copy in `batch` is the failure this repository has
already paid for once: the reference numbering existed twice and the two disagreed the moment a
video was cited (see `models.numbered_references`).

Everything here was `app.py`'s and is unchanged, byte for byte, so every prompt ever built from it
is the prompt it was. `app` re-exports the names it moved out, because `app.reference_map_sentence`
and friends are how the tests and the rest of the module already spell them.

The wording constants at the bottom are the same argument applied to sentences: the refusal
(`app.STALE_REFERENCE_MAP_REFUSAL`) and the pre-flight's note (`batch.SHOT_WITH_STALE_REFERENCE_MAP`)
are one problem described to one Director, so the clauses they share are written once here and
composed at each surface rather than re-typed. `api.js` already keeps `READINESS_REMEDY` apart from
`READINESS_REFUSAL` for exactly this reason: "a Director who reads two different instructions for
one problem tries both".

Depends on `models` and `timeline` only, and nothing may import `app` from here.
"""

from __future__ import annotations

from .models import Project, Shot, numbered_references, song_audio_tag
from .timeline import anchored_label

__all__ = [
    "REFERENCE_MAP_PREFIX",
    "REFERENCE_MAP_ROLE_TAGS",
    "STALE_REFERENCE_MAP_CAUSE",
    "STALE_REFERENCE_MAP_CONSEQUENCE",
    "STALE_REFERENCE_MAP_REMEDY",
    "reference_map_sentence",
    "reference_map_tag_lines",
    "song_audio_prose_expansion",
    "stale_reference_map",
]

#: How the reference map declares a keyframe-role picture, per MiniMax's guide §2.2.2 — read
#: from the bundled ``Video_Prompt_Writing_Guide.pdf``, never copied: the guide's own example is
#: this sentence shape, and the retention marker is the guide's fixed English value for a frame
#: anchor. `[Shot 1]` for the first frame because the guide has `[Shot 1]` mark the opening shot
#: of every prompt; the last frame is tied to *the final shot* in the guide's own alignment
#: language ("the last frame must be reached by the final [Shot N]"), and an un-expanded intent
#: declares no shot numbers this map could name, so the final shot is named as what it is rather
#: than guessed at as an index.
#:
#: A plain reference keeps the exact line this route has always built — `<Picture N> is
#: {label}` — so a shot with no keyframe roles is byte-identical to before these existed.
REFERENCE_MAP_ROLE_TAGS = {
    "first": "<Picture {number}> is the first frame of [Shot 1] (fully_preserved), showing {label}",
    "last": "<Picture {number}> is the last frame of the final shot (fully_preserved), showing {label}",
}


#: How every reference map this application writes opens. Named because three things now read
#: it rather than one writing it: `app.reference_prompt` builds the sentence, `Shot.h3_prompt_map`
#: stores it, and `stale_reference_map` decides from it whether a stored expansion still
#: describes the references the shot cites. The bytes are exactly what the submit route has always
#: emitted — the prefix, the tags joined by `"; "`, a full stop — so every prompt ever built
#: from it is unchanged.
REFERENCE_MAP_PREFIX = "Reference map: "


def reference_map_sentence(tags: list[str]) -> str:
    """The reference map as one sentence: the single construction of it.

    One function rather than an f-string in each of its three readers, because the whole point
    of storing the map beside the expansion is that the stored copy and the live one are
    *comparable* — two spellings of the same join would report every shot as stale.
    """
    return f"{REFERENCE_MAP_PREFIX}{'; '.join(tags)}."


def reference_map_tag_lines(project: Project, shot: Shot) -> list[str]:
    """The submit walk's tag sentences, computed outside the submit route.

    Byte-for-byte the lines `app.generate_h3`'s reference branch builds — same
    `models.numbered_references` walk, same per-kind numbering, same role wording, same
    master-song line last — so a prompt stored ahead of submission names exactly the
    slots the payload will fill. A citation whose asset is missing writes no line here
    where the route 422s: this function writes text, and the render is where a dangling
    citation must stop the world. It still consumes its *number* from the shared walk, so
    the tags that do get written are the tags the specialist was handed.

    Each label carries the Asset's stored appearance anchor when it has one, so the map
    reads `<Picture 1> is Lucy, a woman in a red leather jacket and black boots` rather
    than a bare name that tells the sampler nothing about the person it is holding fixed.
    `timeline.anchored_label` is the one composition — including how a per-shot rename and
    an anchor compose — and an asset with no anchor returns the bare label unchanged, so
    an anchor-free project's map is byte-for-byte the map it has always been.
    """
    tags: list[str] = []
    for numbered in numbered_references(project, shot):
        asset = numbered.asset
        if asset is None:
            continue
        label = anchored_label(asset, shot.reference_labels.get(asset.id, asset.name))
        if numbered.citation.role in REFERENCE_MAP_ROLE_TAGS:
            tags.append(
                REFERENCE_MAP_ROLE_TAGS[numbered.citation.role].format(
                    number=numbered.number, label=label
                )
            )
            continue
        tags.append(f"{numbered.tag} is {label}")
    if shot.use_song_audio:
        tags.append(
            f"<Audio {song_audio_tag(project, shot)}> is the master song for synchronization"
        )
    return tags


def song_audio_prose_expansion(shot: Shot) -> bool:
    """Whether this Shot's stored expansion is the deterministic prose form.

    Read off the *text*, not off the mode, and that is the whole point: `use_song_audio` and
    `mode` say which form the **next** expansion would take, while this says which form the one
    already stored actually is. A song-audio reference shot can be holding a document expansion
    written before the prose recipe existed (`app.song_audio_prose` blanks the field precisely so
    it can build over one), and rewriting that from a rule about the shot rather than a fact about
    the text would throw away model output nobody asked to replace.
    """
    return shot.h3_prompt.startswith(REFERENCE_MAP_PREFIX)


def stale_reference_map(project: Project, shot: Shot) -> bool:
    """Whether this Shot's stored expansion names references the Shot no longer cites.

    **The one implementation**, and the reason this module exists — see the module docstring.
    `app.generate_h3` refuses on it, `app.refresh_reference_maps` re-derives the half it can, and
    `batch.readiness_report` reports it in the pre-flight so a Director reads it before confirming
    a batch rather than as a per-shot skip afterwards. Three readers, one answer, by construction.

    The live defect, 2026-08-20: a shot whose take had invented a woman in a wedding dress got
    the character sheet attached, `Shot.citations` changed, and `h3_prompt` went on carrying the
    old map naming only the bed. `app.reference_prompt` submits a stored expansion **alone**, so
    the next render would have been conditioned on a map that described a different set of
    pictures than the payload wired — which comes back plausible and wrong rather than failing.

    Two answers because there are two kinds of expansion, and each is decided from what can
    actually be known about it:

    * **The prose form carries its own map**, verbatim and at the front, so it is compared
      against the map the shot would get now. Nothing is stored and nothing can be clobbered by
      a client reasserting an old `h3_prompt` — which is exactly what the shots write does,
      since it never adopts its own reply. This is the arm the Director's whole project uses.
    * **A document expansion never writes the map down** — the specialist weaves `<Picture 2>`
      into prose — so it is compared against `Shot.h3_prompt_map`, the map recorded beside it
      when it was written.

    Three states are deliberately *not* stale. A Shot with no expansion has nothing to be stale:
    it has not been expanded, and manufacturing a map for it would invent an expansion. A
    document expansion written before `h3_prompt_map` existed records no map, and nothing knows
    which one it had — refusing that render on a guess is worse than the render, and the next
    expansion records one. And a shot citing nothing at all with no song audio has an empty map,
    which the prose form spells `"Reference map: ."` and compares equal to itself.

    Pure and cheap enough for the pre-flight to ask of every shot on every readiness fetch: one
    `numbered_references` walk over this shot's citations, no I/O, no model, no ComfyUI — and the
    unexpanded shots, which are the ones a plan has most of while it is being written, return on
    the first line without walking anything.
    """
    if not shot.h3_prompt.strip():
        return False
    sentence = reference_map_sentence(reference_map_tag_lines(project, shot))
    if song_audio_prose_expansion(shot):
        return not shot.h3_prompt.startswith(sentence)
    return bool(shot.h3_prompt_map) and shot.h3_prompt_map != sentence


# --------------------------------------------------------------------------------------------
# The clauses both surfaces say. One problem, one Director, one explanation — split into the
# three things a refusal and a pre-flight note genuinely share, so neither can drift from the
# other while the file that holds it is edited for its own reasons.
#
# `app.STALE_REFERENCE_MAP_REFUSAL` composes them into the sentence the submit route raises, and
# `batch.SHOT_WITH_STALE_REFERENCE_MAP` into the sentence the pre-flight prints. The refusal's
# bytes are unchanged by the split, which the frozen-wording tests assert.
# --------------------------------------------------------------------------------------------

#: What has happened to this shot, as a clause the surrounding sentence supplies a subject for
#: ("Not submitted: {shot}'s …", "This shot's …").
STALE_REFERENCE_MAP_CAUSE = (
    "expanded prompt was written against a different set of references than the shot now cites, "
    "so the reference map it carries is stale"
)

#: Why that is worth stopping for. The whole argument for refusing rather than rendering: this is
#: not a failure mode that announces itself, because H3's media slots are anonymous.
STALE_REFERENCE_MAP_CONSEQUENCE = (
    "a render conditioned on a map naming the wrong pictures comes back plausible and wrong "
    "rather than failing"
)

#: What to do about it, in the order that matters.
#:
#: **Clearing the box is named first because it is the remedy that always works.** An empty
#: `h3_prompt` is not a gap — it is the fallback, and `app.reference_prompt` then builds the map
#: from the shot's own citations at submission, so a cleared box submits a map that is correct by
#: construction. It works on a locked shot too, because a lock stops the automated writers and
#: never the human in the inspector (`EXPANSION_LOCKED_NOTICE` says so in as many words).
#:
#: Re-expanding is named second and named *accurately*, with the step it actually needs. Anything
#: reaching the refusal is at `ready` — `generate_h3` refuses every other status — and
#: `shot_render_provenance` counts every status past `draft` as a render, so a document-mode
#: shot's expansion route would answer `EXPAND_PROMPT_RENDERED` if it were pressed from here.
#: `mark-draft` is the existing way back and is deliberately as cheap as arming was.
STALE_REFERENCE_MAP_REMEDY = (
    "Clear the expanded prompt and the render will build the map from the shot's own references, "
    "or send the shot back to draft and expand it again."
)
