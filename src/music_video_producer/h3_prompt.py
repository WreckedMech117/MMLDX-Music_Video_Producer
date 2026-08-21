"""The H3 structured prompt format, and a checker for the rules that are mechanical.

MiniMax's ``Video_Prompt_Writing_Guide.pdf`` (20 pages, bundled with the
``ComfyUI-Fantastic-MiniMaxH3-PromptBuilder`` node pack) documents a structured
prompt format for H3: an optional instruction line, then three named fields, with
shot markers, cut times, speaker ids, dialogue tags and reference tags inside the
first of them. A rewriting model, ``H3-Context-IR``, was supposed to turn a plain
idea into that format. It was never open-sourced, which is why this application
has to produce the format itself.

The guide is third-party and is deliberately **not** copied into this repository.
What lives here are the rules an adapter needs, and only the ones a machine can
actually decide.

**That split is the point of this module.** The guide's own Output Checklist mixes
two kinds of rule:

*Mechanical* — shot numbers in order, cut times strictly increasing and inside the
video, ``[Shot 1]`` carrying no timestamp, ``<d>`` tags balanced and language-tagged,
sentence counts on the two sound fields, no speaker id inside ``retention_analysis``.
Those are checked here, before a render rather than after a bad one.

*Semantic* — every cut introducing new information, only vocalizing characters
carrying a speaker id, amplitude given only where meaningful, ambience kept out of
``overall_soundscape``. **Nothing here can check those**, and pretending otherwise
would be worse than admitting it: they have to live in the specialist's system
prompt, and they are precisely what a model writing from taste gets wrong.

So a prompt passing every check in this module is well-*formed*, not well-*written*.
Callers must not report it as approved.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

#: The three core fields, in the order the guide requires them.
CORE_FIELDS: tuple[str, ...] = (
    "integrated_multimodal_description",
    "overall_soundscape",
    "non_diegetic_music",
)

#: `overall_soundscape` is one to four sentences; `non_diegetic_music` one to three.
#: Both from the guide's checklist. The lower bound is 1 rather than 0 because an
#: empty field is a different error (a missing field) and is reported as that.
SENTENCE_BOUNDS: dict[str, tuple[int, int]] = {
    "overall_soundscape": (1, 4),
    "non_diegetic_music": (1, 3),
}

#: A value the guide permits where a field genuinely does not apply. It is exempt
#: from the sentence bounds; whether it is *warranted* is a judgement no checker makes.
NOT_APPLICABLE = "N/A"

#: One cut time, `At MM:SS.mmm`, as a fragment shared by `_SHOT` and `_ANY_CUT`.
#:
#: **Both patterns are built from this one string, deliberately.** They have to tokenize a cut
#: time identically or `check_orphan_cuts` compares two different tokenizations and invents
#: orphans: it decides a time "belongs to a shot" by matching the offset `_SHOT` recorded for it
#: against the offset `_ANY_CUT` found, so any drift between the two turns a correctly marked
#: cut into a fatal false positive. Two literals that must agree forever is how they stop
#: agreeing.
#:
#: `At`, `at`, `AT` all match, and that is the whole point (2026-08-20). Matching only the
#: guide's capital `At` meant `at 00:06.250` — the casing the local models actually produce, 19
#: of 28 timecodes in that day's captured expansions — was invisible to every cut-time check
#: here. A real accepted expansion carried five lowercase prose timestamps under one `[Shot 1]`,
#: one of them past the end of the clip, and the checker passed it as well-formed. Nothing is
#: lost by the leniency: `\d{2}:\d{2}\.\d{3}` is an unambiguous timecode, so a model that writes
#: one after the word "at" means a cut time whatever shift key it held, and a human reading the
#: prompt reads one too. The leading `\b` keeps it from firing inside a word — "format
#: 00:05.000" is not a cut.
_CUT_TIME = r"\b(?i:At)\s+(\d{2}):(\d{2})\.(\d{3})"

#: `[Shot 1]`, or `[Shot N] At MM:SS.mmm`. Shot 1 must not carry a timestamp, which
#: is checked rather than encoded here — a stricter pattern would make the violation
#: unparseable instead of reportable, and an unreportable error is a worse outcome.
#:
#: `Shot` stays case-sensitive on purpose, unlike the `At` inside `_CUT_TIME`. The two have
#: opposite failure modes: an unmatched `at` was a *silent pass*, while `[shot 2]` already fails
#: loudly — `_SHOT` finds no marker and the checker refuses the prompt. Relaxing `Shot` would
#: convert that loud refusal into a pass, and H3 reads shot boundaries off this marker; nothing
#: here knows whether its parser accepts a lower-case one, so the checker must not assume it
#: does. Leniency is safe where it only widens what gets *reported*, not where it widens what
#: gets accepted.
_SHOT = re.compile(rf"\[Shot (\d+)\](?:\s*{_CUT_TIME})?")

#: `<d>` and `</d>`, case-insensitively. A `<D>` block used to defeat all three dialogue checks
#: at once — the balance count saw zero tags so it balanced, the language-tag scan found no
#: block to inspect, and `forbid_dialogue`'s song-audio guard did not fire. That last one is the
#: guard written on 2026-08-19 because the model invented well-formed lyrics onto a shot riding
#: the master song; a shift key would have walked straight through it. Same class of bug as the
#: cut times, found by auditing rather than by another bad render.
_DIALOGUE_OPEN = re.compile(r"<d>", re.IGNORECASE)
_DIALOGUE_CLOSE = re.compile(r"</d>", re.IGNORECASE)
#: Each `<d>` must open with a bracketed language tag, e.g. `<d>[English] ...`.
_DIALOGUE_BLOCK = re.compile(r"<d>\s*(\[[^\]]+\])?", re.DOTALL | re.IGNORECASE)
#: `(S1)`, `(S1, S2)`. Case-insensitive for `check_retention`'s sake: a lower-case `(s1)` sitting
#: in `retention_analysis` is exactly the mistake that check exists to catch, and reading only
#: the guide's capital would have let it through in silence.
_SPEAKER = re.compile(r"\(S(\d+)(?:\s*,\s*S(\d+))*\)", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")

#: Any cut time, wherever it appears. Used to find cut times that carry no `[Shot N]`
#: in front of them — see `check_orphan_cuts`.
_ANY_CUT = re.compile(_CUT_TIME)


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a prompt.

    ``fatal`` separates "this will not do what you meant" from "this is worth a
    look". The distinction matters because the two want different handling: a fatal
    problem should stop a submission, an advisory one should be shown and ignored if
    the Director disagrees. Collapsing them would either block on advice or submit
    through errors.
    """

    field: str
    message: str
    fatal: bool = True


@dataclass
class ParsedPrompt:
    """What a prompt turned out to contain. Absent fields are simply missing keys."""

    instruction: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    @property
    def fatal(self) -> list[Problem]:
        return [problem for problem in self.problems if problem.fatal]

    @property
    def well_formed(self) -> bool:
        """No fatal problems. **Not** a statement that the prompt is any good."""
        return not self.fatal


def _count_sentences(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    ends = len(_SENTENCE_END.findall(stripped))
    # Text that never terminates still contains one sentence's worth of content;
    # counting 0 would report "too few" for a field that is merely unpunctuated.
    return ends if ends else 1


def parse(prompt: str) -> ParsedPrompt:
    """Split a prompt into its instruction line and three fields.

    The guide's worked examples put a field's value either directly after its colon
    or on the line beneath it, so both are accepted. Everything up to the first field
    label is the instruction; T2VA has none and that is not an error here, because
    whether an instruction is *required* depends on the mode the caller asked for.
    """
    parsed = ParsedPrompt()
    names = "|".join(CORE_FIELDS)
    labels = [(match.start(), match.group(1)) for match in
              re.finditer(rf"^({names})\s*:", prompt, re.MULTILINE)]

    # A label appearing mid-line is *present but unparseable*, and calling it "missing"
    # would send a reader hunting for something already in front of them. A local model
    # asked for three fields each on its own line will run them together on one — seen
    # on the first live run — and the fix is a line break rather than a rewrite, so the
    # message should say which it is.
    inline = sorted(
        {match.group(1) for match in re.finditer(rf"(?<!^)\b({names})\s*:", prompt,
                                                 re.MULTILINE)}
        - {name for _, name in labels}
    )
    for name in inline:
        parsed.problems.append(Problem(
            name, f"{name} appears mid-line; each field must start its own line."))

    if not labels:
        parsed.problems.append(
            Problem("prompt", "No core fields found; expected "
                    f"{CORE_FIELDS[0]}, {CORE_FIELDS[1]} and {CORE_FIELDS[2]}."
                    + (" They are present but run together on one line."
                       if inline else ""))
        )
        parsed.instruction = prompt.strip()
        return parsed

    parsed.instruction = prompt[: labels[0][0]].strip()
    for index, (start, name) in enumerate(labels):
        end = labels[index + 1][0] if index + 1 < len(labels) else len(prompt)
        body = prompt[start:end]
        _, _, value = body.partition(":")
        if name in parsed.fields:
            parsed.problems.append(Problem(name, f"{name} appears more than once."))
            continue
        parsed.fields[name] = value.strip()

    present = [name for _, name in labels]
    ordered = [name for name in CORE_FIELDS if name in parsed.fields]
    if present != ordered:
        parsed.problems.append(
            Problem("prompt", "The core fields are out of order; the guide requires "
                    f"{', then '.join(CORE_FIELDS)}.")
        )
    return parsed


def check_shots(description: str, *, duration: float | None = None) -> list[Problem]:
    """Shot numbering, cut-time monotonicity, and the Shot 1 timestamp rule."""
    problems: list[Problem] = []
    matches = list(_SHOT.finditer(description))
    if not matches:
        problems.append(Problem(CORE_FIELDS[0], "No [Shot 1] opening."))
        return problems

    previous_ms = -1
    for position, match in enumerate(matches, start=1):
        number = int(match.group(1))
        stamped = match.group(2) is not None
        if number != position:
            problems.append(Problem(
                CORE_FIELDS[0],
                f"[Shot {number}] is the {position}th marker; shots must be numbered "
                "in order with none skipped or repeated.",
            ))
        if number == 1 and stamped:
            problems.append(Problem(
                CORE_FIELDS[0], "[Shot 1] must not carry a timestamp."))
        if number > 1 and not stamped:
            problems.append(Problem(
                CORE_FIELDS[0], f"[Shot {number}] has no cut time; every shot after "
                "the first needs one as At MM:SS.mmm."))
        if not stamped:
            continue
        ms = (int(match.group(2)) * 60_000 + int(match.group(3)) * 1000
              + int(match.group(4)))
        if ms <= previous_ms:
            problems.append(Problem(
                CORE_FIELDS[0],
                f"[Shot {number}]'s cut time does not advance; cut times must "
                "strictly increase.",
            ))
        if duration is not None and ms >= duration * 1000:
            problems.append(Problem(
                CORE_FIELDS[0],
                f"[Shot {number}] cuts at {ms / 1000:.3f}s, at or beyond the "
                f"{duration:.2f}s the shot is being rendered for.",
            ))
        previous_ms = ms
    return problems


def check_orphan_cuts(text: str, *, field: str = CORE_FIELDS[0],
                      duration: float | None = None) -> list[Problem]:
    """Cut times that carry no `[Shot N]` in front of them, and stray cuts past the clip's end.

    Found by measurement rather than by reading the guide: a model asked for a short clip
    wrote `[Shot 1] … At 00:02.500 A grey wolf steps … At 00:03.750 Close on her face`, and
    every other check here passed it. It looked like a three-shot prompt and is not one — H3
    reads shot boundaries from `[Shot N]`, so those times are prose. The clip would render as
    one continuous shot with two stray timestamps described inside it.

    This is worth checking precisely because it is invisible to the eye that wrote it: the
    intent is legible to a human reader, which is what makes it easy to ship.

    ``field`` names the field being scanned, because **cut times stray into the sound fields
    too**. Measured on 2026-08-20: one accepted expansion wrote three prose timestamps into
    the description and one more into each of `overall_soundscape` and `non_diegetic_music`.
    A time in a sound field is not a weaker version of the same mistake — it is a stronger
    one, because H3 reads no timing from those fields at all, so it cannot even be misread as
    a cut. Scanning only the description made two thirds of that prompt's fields exempt.

    ``duration`` reports a **second and separate** Problem for a stray time at or past the end
    of the clip. That is deliberately not folded into the orphan sentence: they are different
    defects with different fixes. An orphan says the model wrote prose where a marker belongs
    and the fix is `[Shot N]`; a time past the end says the model has lost the clip's length
    and the fix is a different number — a cut that would still be wrong after a marker was
    added in front of it. `at 00:06.250` on the 5.665s clip that exposed this was both, and
    reporting one of the two would have sent the retry loop after half the problem. Only
    *unmarked* times are bounds-checked here; `check_shots` already owns every time a shot
    marker claims, and reporting it twice would be noise rather than information.
    """
    problems: list[Problem] = []
    marked = {match.start(2) for match in _SHOT.finditer(text)
              if match.group(2) is not None}
    for cut in _ANY_CUT.finditer(text):
        if cut.start(1) in marked:
            continue
        stamp = cut.group(0)
        if field == CORE_FIELDS[0]:
            problems.append(Problem(
                field,
                f"'{stamp}' has no [Shot N] in front of it, so it is prose rather than a "
                "cut. Every cut time belongs to a shot marker.",
            ))
        else:
            problems.append(Problem(
                field,
                f"'{stamp}' is a cut time written into {field}. H3 takes shot boundaries only "
                f"from [Shot N] in {CORE_FIELDS[0]}, and reads no timing out of the sound "
                "fields at all, so this cuts nothing — it is prose. Move the timing to a shot "
                "marker, or describe the sound without a timestamp.",
            ))
        ms = (int(cut.group(1)) * 60_000 + int(cut.group(2)) * 1000
              + int(cut.group(3)))
        if duration is not None and ms >= duration * 1000:
            problems.append(Problem(
                field,
                f"'{stamp}' is at {ms / 1000:.3f}s, at or beyond the {duration:.2f}s this "
                "shot is being rendered for; there is no such moment in the clip.",
            ))
    return problems


def check_dialogue(description: str) -> list[Problem]:
    """`<d>` tags balanced, and each opening one carrying a language tag."""
    problems: list[Problem] = []
    opens = len(_DIALOGUE_OPEN.findall(description))
    closes = len(_DIALOGUE_CLOSE.findall(description))
    if opens != closes:
        problems.append(Problem(
            CORE_FIELDS[0],
            f"Dialogue tags are unbalanced: {opens} <d> against {closes} </d>.",
        ))
    for match in _DIALOGUE_BLOCK.finditer(description):
        if match.group(1) is None:
            problems.append(Problem(
                CORE_FIELDS[0],
                "A <d> block has no language tag; the guide expects <d>[English] "
                "followed by the verbatim speech.",
            ))
    return problems


def check_sound_fields(
    fields: dict[str, str],
    *,
    already_reported: frozenset[str] = frozenset(),
    require: bool = True,
) -> list[Problem]:
    """Sentence bounds on the two sound fields, with `N/A` exempt.

    ``already_reported`` names fields whose absence from ``fields`` has a better
    explanation elsewhere — currently one found mid-line. Reporting such a field as
    *missing* on top of that would hand the reader two contradictory sentences about
    the same field, and the wrong one is the more alarming.
    """
    problems: list[Problem] = []
    for name, (low, high) in SENTENCE_BOUNDS.items():
        if name not in fields and name in already_reported:
            continue
        value = fields.get(name)
        if value is None:
            if require:
                problems.append(Problem(name, f"{name} is missing."))
            continue
        if value.strip() == NOT_APPLICABLE:
            continue
        count = _count_sentences(value)
        if count == 0:
            problems.append(Problem(name, f"{name} is empty."))
        elif count < low or count > high:
            problems.append(Problem(
                name,
                f"{name} is {count} sentences; the guide asks for {low} to {high}.",
                fatal=False,
            ))
    return problems


def check_retention(prompt: str) -> list[Problem]:
    """No speaker id inside `retention_analysis`.

    Speaker ids belong to the description. The guide states this as its own
    checklist line, which is a strong hint that it is a mistake people make.

    The label is matched case-insensitively — unlike `CORE_FIELDS`, which `parse` and
    `_FIELD_LINE` both read exactly. The asymmetry is the difference between the two
    failure modes: a mis-cased core field is refused outright as "no core fields found",
    while a `Retention_Analysis:` block would simply not be scanned, and its speaker id
    would ship. Silence is the outcome worth closing.
    """
    match = re.search(r"^retention_analysis\s*:(.*?)(?=^\w+\s*:|\Z)",
                      prompt, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    if not match:
        return []
    found = _SPEAKER.findall(match.group(1))
    if found:
        return [Problem(
            "retention_analysis",
            "retention_analysis contains a speaker id; those belong in the "
            "description.",
        )]
    return []


#: The three tag namespaces H3 numbers **independently**. The conditioner wires pictures, videos
#: and audios into three separate per-kind slot lists, so `<Picture 2>` and `<Video 2>` are two
#: different slots and neither of them is "the second reference". `app.reference_map_tag_lines`
#: and the submit route both number their tags from three separate counters for this reason, and
#: a checker sharing one counter across the kinds would report false bounds on any mixed shot.
REFERENCE_TAG_NAMES: dict[str, str] = {
    "picture": "Picture",
    "video": "Video",
    "audio": "Audio",
}

#: The field name reference-slot problems are reported under. Not one of `CORE_FIELDS`: a tag may
#: legitimately appear in the instruction line, the description or `non_diegetic_music` (the
#: guide's own §2.6 reuse declaration cites `<Audio N>` there), so the problem is about the
#: prompt's references rather than about any one field.
REFERENCE_FIELD = "references"

#: `<Picture 3>`, `<Video 1>`, `<Audio 2>`. Case-insensitive because what is being bounded is the
#: *number*: a model that wrote `<picture 3>` onto a two-picture shot made exactly the mistake this
#: catches, and matching only the guide's capitalisation would let it through.
#:
#: `<Subject N>` is deliberately **absent**. A subject is defined by the prompt itself — see
#: `workflows.py`'s `<Subject 1> is the character from <Picture 1>` — and is wired into no payload
#: slot at all, so it has no count it could be out of bounds of. Bounding it would refuse valid
#: prompts, which is the one outcome worse than not checking.
_REFERENCE_TAG = re.compile(r"<(Picture|Video|Audio)\s+(\d+)\s*>", re.IGNORECASE)


def check_reference_bounds(prompt: str, *, slots: Mapping[str, int]) -> list[Problem]:
    """Every reference tag the prompt cites, against the slots the payload will actually wire.

    `slots` maps `REFERENCE_TAG_NAMES`' keys to the number of that kind the render will attach.
    **A kind the mapping omits is skipped entirely**, in both directions, and that is the
    contract's most important clause: the caller is the only thing that knows how a mode wires
    its media, and a caller that cannot count a kind reliably must leave it out rather than pass
    a guess. A wrong count here refuses a valid shot, and a gate that refuses valid work is worse
    than no gate — it gets switched off, and it takes the true refusals with it.

    Two directions, deliberately at two severities, matching how `batch.readiness_report` splits
    emptiness from sameness:

    * **Prompt → slots is fatal.** H3's media slots are anonymous: the prompt's `<Picture N>` *is*
      the Nth picture the payload appends, and nothing else names it. A tag past the last attached
      picture therefore conditions a full GPU pass on a slot nothing fills, and the take comes back
      plausible and wrong rather than failing. `<Picture 0>` is the same defect from the other end —
      the numbering starts at 1.
    * **Slots → prompt warns and never blocks.** A picture wired in but never mentioned is weak
      evidence: a style or lighting reference the Director attached on purpose has nothing useful to
      say about it in prose, and blocking that would refuse a legitimate shot. It is still worth
      saying, because the far more common cause is a prompt written against a different attachment
      set.
    """
    cited: dict[str, set[int]] = {kind: set() for kind in REFERENCE_TAG_NAMES}
    for match in _REFERENCE_TAG.finditer(prompt):
        cited[match.group(1).lower()].add(int(match.group(2)))

    problems: list[Problem] = []
    for kind, name in REFERENCE_TAG_NAMES.items():
        if kind not in slots:
            continue
        count = slots[kind]
        plural = f"{kind}s"
        for number in sorted(cited[kind]):
            if number < 1:
                problems.append(Problem(
                    REFERENCE_FIELD,
                    f"<{name} {number}> cites slot {number}; reference slots are numbered from "
                    f"1, so there is no such {kind}.",
                ))
            elif number > count:
                have = (
                    f"this shot has no {plural} attached at all"
                    if count == 0
                    else f"only {count} {plural if count > 1 else kind} "
                    f"{'are' if count > 1 else 'is'} attached, so <{name} {count}> is the "
                    f"highest tag this shot can carry"
                )
                problems.append(Problem(
                    REFERENCE_FIELD,
                    f"<{name} {number}> is cited, but {have}. H3's media slots are anonymous — "
                    f"the prompt's <{name} N> is the Nth {kind} the payload wires and nothing "
                    f"else names it — so this tag conditions the render on a slot nothing fills. "
                    f"Attach the {kind}, or renumber the tag.",
                ))
        for number in range(1, count + 1):
            if number in cited[kind]:
                continue
            problems.append(Problem(
                REFERENCE_FIELD,
                f"<{name} {number}> is attached and wired into the payload, but the prompt never "
                f"mentions it. The render is conditioned on media nothing in the prompt accounts "
                f"for. This may be deliberate and does not block the render.",
                fatal=False,
            ))
    return problems


def check(prompt: str, *, duration: float | None = None,
          expect_instruction: bool = False,
          require_sound_fields: bool = True,
          forbid_dialogue: bool = False,
          reference_slots: Mapping[str, int] | None = None) -> ParsedPrompt:
    """Check the mechanical rules and report what is wrong.

    ``duration`` bounds the cut times when the caller knows the shot's length — both the
    ones a `[Shot N]` marker claims and the stray ones that belong to no marker, which are
    reported separately because they are separate defects; see `check_orphan_cuts`.
    ``expect_instruction`` is for the keyframe modes, which require an instruction
    line as the first line. T2VA requires its absence, and that is checked too —
    an instruction on a T2VA prompt is a mode confusion worth catching.

    ``require_sound_fields=False`` is for prompts whose two audio fields were removed
    rather than normalized. Fields *present* on such a prompt are still bounds-checked;
    only their absence stops being a problem.

    ``forbid_dialogue=True`` is for song-audio shots, and it exists because the rule it
    enforces failed as a prohibition: told never to invent sung words, the model on this
    machine wrote a fully well-formed `<d>[English] "I'm howling at the moon..."` — words
    that exist nowhere in the song — onto a not-singing shot, and every other check
    passed it (2026-08-19). On a shot conditioned on the master song, ANY `<d>` block is
    a second vocal source fighting the reference, so its presence is the defect, and
    flagging it here is what routes the sentence into the expansion retry loop.

    ``reference_slots`` is the per-kind count of media the render will wire, and ``None`` — the
    default — skips the reference-bounds pass entirely, which is what every caller that cannot
    count the slots must pass. See `check_reference_bounds` for the two directions and why the
    over-citation half is fatal and the under-citation half is not.

    A clean result means well-formed. It does not mean good; see the module
    docstring for what deliberately is not checked here.
    """
    parsed = parse(prompt)
    if expect_instruction and not parsed.instruction:
        parsed.problems.append(Problem(
            "instruction",
            "This mode requires an instruction line naming how each picture aligns "
            "to a time in the target video, as the first line.",
        ))
    if not expect_instruction and parsed.instruction:
        parsed.problems.append(Problem(
            "instruction",
            "Text before the first core field, but this mode takes no instruction "
            "line.",
            fatal=False,
        ))

    description = parsed.fields.get(CORE_FIELDS[0])
    if description is None:
        parsed.problems.append(Problem(CORE_FIELDS[0], f"{CORE_FIELDS[0]} is missing."))
    else:
        parsed.problems.extend(check_shots(description, duration=duration))
        parsed.problems.extend(check_orphan_cuts(description, duration=duration))
        parsed.problems.extend(check_dialogue(description))
        if forbid_dialogue and _DIALOGUE_OPEN.search(description):
            parsed.problems.append(Problem(
                CORE_FIELDS[0],
                "This shot rides the master song, so the description may contain no "
                "<d> dialogue block at all: the referenced audio carries every word, "
                "and written speech or lyrics would fight it. Remove the <d> block and "
                "its contents; describe the performance as visible action instead.",
            ))
    # The two sound fields, for stray cut times only. The instruction line is deliberately
    # **not** scanned: its whole job is to name a time in the target video, so a timecode
    # there may be exactly what the mode asked for, and a gate that refuses the correct form
    # gets switched off along with its true refusals.
    for name in CORE_FIELDS[1:]:
        value = parsed.fields.get(name)
        if value:
            parsed.problems.extend(
                check_orphan_cuts(value, field=name, duration=duration))
    reported = frozenset(problem.field for problem in parsed.problems)
    parsed.problems.extend(
        check_sound_fields(
            parsed.fields, already_reported=reported, require=require_sound_fields
        )
    )
    parsed.problems.extend(check_retention(prompt))
    if reference_slots is not None:
        # Over the whole prompt rather than over one field: a reference tag is legal in the
        # instruction line, in the description and in `non_diegetic_music` alike.
        parsed.problems.extend(check_reference_bounds(prompt, slots=reference_slots))
    return parsed


#: The core field labels, matched **exactly**, and the one pattern in this module that must
#: stay that way. It is the only regex here that drives a *rewrite* rather than a report —
#: `normalize_audio_fields` cuts payload bytes on its spans — so loosening it would change what
#: a submitted payload contains, which no checker is allowed to do. `parse` reads the same
#: labels with the same exactness for the second half of the reason: if the two disagreed about
#: what counts as a field label, the checker and the rewriter would be reading two different
#: documents out of one string. A mis-cased label is not a silent hole either way — `parse`
#: finds no labels and refuses the prompt outright.
_FIELD_LINE = re.compile(
    rf"^({'|'.join(CORE_FIELDS)}):", re.MULTILINE
)


#: The guide's own reuse declaration (§2.6): the audience-only score IS the referenced
#: track, said by tag so the model can link the sentence to the audio slot — the link the
#: failed deferral prose ("the referenced master song") never made.
SONG_AUDIO_MUSIC_TEMPLATE = (
    "<Audio {tag}> is directly reused as the complete audience-only score."
)


def normalize_audio_fields(prompt: str, *, audio_tag: int = 1) -> str:
    """Rewrite a song-audio shot's two audio fields to the guide's reuse declaration.

    The measurement history (2026-08-19, one shot family, same seeds): the specialist's
    freely-written fields drowned the referenced track — envelope correlation with the
    master's actual window 0.36 (turbo) / 0.27 (20 steps); untagged deferral prose
    recovered unreliably (0.36–0.73 — the model synthesizes from any text it cannot
    ground); bare absence measured 0.84. The guide's §2.6 form is the official shape:
    `non_diegetic_music` cites the audio **by tag** as the directly-reused score, and
    `overall_soundscape` — which must never repeat singing or music — goes `N/A`, the
    checker-exempt explicit silence. Singing itself lives in the description, where the
    specialist writes it as visible action.

    The description is untouched byte for byte; a prompt missing both fields comes back
    unchanged (the checker owns malformed documents); idempotent by construction.
    """
    matches = list(_FIELD_LINE.finditer(prompt))
    spans: dict[str, tuple[int, int]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(prompt)
        spans[match.group(1)] = (match.end(), end)
    if CORE_FIELDS[1] not in spans and CORE_FIELDS[2] not in spans:
        return prompt
    replacements = {
        CORE_FIELDS[1]: NOT_APPLICABLE,
        CORE_FIELDS[2]: SONG_AUDIO_MUSIC_TEMPLATE.format(tag=audio_tag),
    }
    rebuilt = prompt
    for name in (CORE_FIELDS[2], CORE_FIELDS[1]):  # back-to-front keeps spans valid
        if name in spans:
            start, end = spans[name]
            body = rebuilt[start:end]
            trailing = body[len(body.rstrip()):]
            rebuilt = rebuilt[:start] + " " + replacements[name] + trailing + rebuilt[end:]
    return rebuilt
