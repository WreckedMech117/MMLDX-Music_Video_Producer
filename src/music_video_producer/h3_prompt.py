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

#: `[Shot 1]`, or `[Shot N] At MM:SS.mmm`. Shot 1 must not carry a timestamp, which
#: is checked rather than encoded here — a stricter pattern would make the violation
#: unparseable instead of reportable, and an unreportable error is a worse outcome.
_SHOT = re.compile(r"\[Shot (\d+)\](?:\s*At (\d{2}):(\d{2})\.(\d{3}))?")

_DIALOGUE_OPEN = re.compile(r"<d>")
_DIALOGUE_CLOSE = re.compile(r"</d>")
#: Each `<d>` must open with a bracketed language tag, e.g. `<d>[English] ...`.
_DIALOGUE_BLOCK = re.compile(r"<d>\s*(\[[^\]]+\])?", re.DOTALL)
_SPEAKER = re.compile(r"\(S(\d+)(?:\s*,\s*S(\d+))*\)")
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


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
    labels = [(match.start(), match.group(1)) for match in
              re.finditer(rf"^({'|'.join(CORE_FIELDS)})\s*:", prompt, re.MULTILINE)]
    if not labels:
        parsed.problems.append(
            Problem("prompt", "No core fields found; expected "
                    f"{CORE_FIELDS[0]}, {CORE_FIELDS[1]} and {CORE_FIELDS[2]}.")
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


def check_sound_fields(fields: dict[str, str]) -> list[Problem]:
    """Sentence bounds on the two sound fields, with `N/A` exempt."""
    problems: list[Problem] = []
    for name, (low, high) in SENTENCE_BOUNDS.items():
        value = fields.get(name)
        if value is None:
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
    """
    match = re.search(r"^retention_analysis\s*:(.*?)(?=^\w+\s*:|\Z)",
                      prompt, re.MULTILINE | re.DOTALL)
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


def check(prompt: str, *, duration: float | None = None,
          expect_instruction: bool = False) -> ParsedPrompt:
    """Check the mechanical rules and report what is wrong.

    ``duration`` bounds the cut times when the caller knows the shot's length;
    ``expect_instruction`` is for the keyframe modes, which require an instruction
    line as the first line. T2VA requires its absence, and that is checked too —
    an instruction on a T2VA prompt is a mode confusion worth catching.

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
        parsed.problems.extend(check_dialogue(description))
    parsed.problems.extend(check_sound_fields(parsed.fields))
    parsed.problems.extend(check_retention(prompt))
    return parsed
