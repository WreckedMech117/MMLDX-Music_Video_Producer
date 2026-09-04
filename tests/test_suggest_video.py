"""Suggest Video: a complete idea proposed from the song, and the ways it does not work.

Story 13.1 and the headless half of 13.2 — TP-3, TP-5, and every failure-path requirement of
TP-4. Its own module for `test_planning.py`'s reason: the guarantees span the wire schema, a pure
composer, a retry loop and a route, and they belong to one feature.

**The happy path is one call. Almost everything below is an unhappy one**, because AD-39 is a list
of unhappy paths and every clause of it was measured rather than imagined:

* reasoning length varies **26x across identical rolls**, so a second roll is a different roll and
  the retry is worth exactly one;
* nothing is written until the reply validates, so a failed pass leaves the Brief **byte-identical**
  — asserted here by comparing the manifest's bytes across a failure, never by reading the route;
* a thin-but-valid reply is **stored and reported as partial**;
* a failure is reported by exception class and elapsed time, never by its string, because
  `httpx.ReadTimeout` stringifies to `""`.

`test_api.py`'s `make_client` is imported rather than duplicated: the route half is only really
tested through the application it is registered on.
"""

import json
import pathlib

import httpx
import pytest
from test_api import FakeDirector, make_client

from music_video_producer.app import (
    DOCUMENT_LABELS,
    DOCUMENT_LOCK_NOTICE,
    DOCUMENT_WRITER_MACHINE,
    DOCUMENT_WRITER_SAVE,
    RECOVERY_SLOT_SUFFIX,
    SAVE_CAPTURED_DOCUMENTS,
    SUGGEST_VIDEO_ATTEMPTS,
    SuggestVideoOutcome,
    run_suggest_video,
    suggest_video_notice,
    suggest_video_refusal,
    write_document,
)
from music_video_producer.director import (
    BRIEF_SECTIONS,
    SUGGEST_VIDEO_TOOL,
    DirectorClient,
    DirectorError,
    DirectorUnavailable,
    SuggestedBrief,
    brief_shortfall,
    compose_brief,
    failure_description,
    parse_suggested_brief,
    suggest_video_tool,
)
from music_video_producer.models import Project, Song, SongSection
from music_video_producer.planning_prompt import SUGGEST_VIDEO_SYSTEM_PROMPT
from music_video_producer.timeline import suggest_video_input

SUGGEST = "/api/projects/{project}/brief/suggest"

LYRICS = "[Verse]\nHeadlights on the county road\n[Chorus]\nAnd the trees close over me"
STYLE = "Sodium amber, hard backlight, 35mm grain. Nothing daylit."

#: A complete reply: five sections, each with something in it.
FULL = {
    "premise": "A driver leaves the last town and keeps going until the road stops being a road.",
    "cast": "One driver, mid-thirties, alone. One wolf, seen four times and never twice the same.",
    "locations": "A county highway at 2am. A gravel turnout. A treeline. The forest past it.",
    "arc": "The verses are the corridor of headlights; the chorus is the treeline swallowing it.",
    "look": "Sodium amber against deep blacks, anamorphic, 35mm grain. Never daylight.",
}


# ---------------------------------------------------------------------------------------------
# Doubles and fixtures
# ---------------------------------------------------------------------------------------------


def call(**arguments: object) -> dict:
    """One `suggest_video` tool call, as a provider puts it on the wire.

    Arguments verbatim, so a test can *drop* a field or send a blank one — those are two different
    replies and the whole partial/malformed distinction is which one arrived.
    """
    return {
        "type": "function",
        "function": {"name": SUGGEST_VIDEO_TOOL, "arguments": json.dumps(arguments)},
    }


def reply(*calls: dict, content: str | None = None) -> dict:
    """One provider message. `content` is `None` by default, which is what a tool reply carries."""
    return {"content": content, "tool_calls": list(calls)}


def suggestion(**overrides: str) -> SuggestedBrief:
    return SuggestedBrief(**{**FULL, **overrides})


class SuggestingDirector(FakeDirector):
    """Answers with fixed suggestions or errors, recording every call it was handed.

    `PlanningDirector`'s pattern on the long-pass surface, with one difference that is the point of
    the module: the answers are consumed **one per call** rather than clamped to the last, because
    *"retries exactly once"* is a claim about a sequence of calls and a double that answers the
    same way forever cannot tell one attempt from three. `calls` is the count every retry
    assertion here is made against.
    """

    def __init__(self, *answers: SuggestedBrief | Exception):
        self.answers = list(answers) or [suggestion()]
        self.inputs: list[dict] = []

    @property
    def calls(self) -> int:
        return len(self.inputs)

    async def suggest_video(self, *, brief_input):
        self.inputs.append(brief_input)
        answer = self.answers[min(len(self.inputs) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def suggestable_project(store, *, brief: str = "", **song: object) -> Project:
    """A project Suggest Video can run on: a song with words and a style, and nothing else."""
    project = store.create(Project(name="Suggest"))
    project.creative_brief = brief
    project.song = Song(
        **{
            "title": "Signal Bloom",
            "source": "imported",
            "duration": 202.0,
            "lyrics": LYRICS,
            "caption": STYLE,
            **song,
        }
    )
    return store.save(project)


def manifest(store, project_id: str) -> bytes:
    """The stored manifest's bytes. What "byte-identical" is asserted against."""
    return store.manifest_path(project_id).read_bytes()


# ---------------------------------------------------------------------------------------------
# The wire schema: five fields, all required, and no way to write anything else
# ---------------------------------------------------------------------------------------------


def test_the_surface_is_one_tool_that_requires_every_field_it_declares():
    """AD-38 on a surface of one. The required list is the load-bearing part, not the prompt.

    On this model an optional field and a dropped field are the same bytes — `DirectorResult` never
    requiring `shots` was the root cause of every empty-shots failure — so a field left out of the
    promotion here would arrive as a smaller answer rather than a broken one.
    """
    tool = suggest_video_tool()
    assert tool["type"] == "function"
    assert tool["function"]["name"] == SUGGEST_VIDEO_TOOL
    assert tool["function"]["description"].strip()
    parameters = tool["function"]["parameters"]
    assert set(parameters["properties"]) == set(BRIEF_SECTIONS)
    assert set(parameters["required"]) == set(BRIEF_SECTIONS)
    # Property order, so the constrained decoder fills the premise before it invents a look.
    assert list(parameters["properties"]) == list(BRIEF_SECTIONS)
    assert parameters["required"] == list(BRIEF_SECTIONS)
    for field in BRIEF_SECTIONS:
        assert parameters["properties"][field]["description"].strip(), field


def test_the_tool_has_no_field_that_could_write_anything_but_the_brief():
    """TP-3 and TP-10, made structural rather than checked (spec constraint 1).

    "It writes the Brief and nothing else — no Treatment, no Style Bible, no Shots, no Assets" is
    enforced by the tool *having no field for them*. A check in a route is a check somebody can
    forget, move or weaken; a key the model has no way to send is none of those things.
    """
    fields = set(suggest_video_tool()["function"]["parameters"]["properties"])
    assert fields == set(SuggestedBrief.model_fields)
    for absent in ("treatment", "style_bible", "shots", "assets", "creative_brief"):
        assert absent not in fields, absent


def test_the_section_mapping_and_the_argument_model_cannot_disagree():
    """Both directions of the one mapping, through machinery that already raises.

    `BRIEF_SECTIONS` names a field the model lacks -> `_promoted` raises. `SuggestedBrief` grows a
    field the mapping lacks -> `_refuse_optional_fields` raises, because the promotion is built
    from the mapping.

    **And both raise at import, which is a claim about *when* and needs its own assertion.**
    `suggest_video_tool()` is a function; nothing about defining it runs the promotion. What runs
    it is `SUGGEST_VIDEO_TOOL_NAMES` building the surface at module scope, exactly as
    `PLANNING_TOOL_NAMES` does — so the last line here is not decoration, it is the difference
    between the application refusing to start and shipping a tool whose grammar is looser than its
    docstring.
    """
    from music_video_producer.director import SUGGEST_VIDEO_TOOL_NAMES, _strict_tool_schema

    assert tuple(BRIEF_SECTIONS) == tuple(SuggestedBrief.model_fields)
    with pytest.raises(ValueError, match="no field"):
        _strict_tool_schema(SuggestedBrief, require=(*BRIEF_SECTIONS, "treatment"))
    with pytest.raises(ValueError, match="premise"):
        _strict_tool_schema(SuggestedBrief, require=tuple(BRIEF_SECTIONS)[1:])
    # Read back off the surface that ships, not off the literal, which is what makes the build
    # happen at import at all.
    assert SUGGEST_VIDEO_TOOL_NAMES == (SUGGEST_VIDEO_TOOL,)


def test_a_section_dropped_from_the_promotion_stops_the_application_from_starting():
    """The word **import** in the test above is a claim about *when*, so it is executed here.

    This was written because the mutation that proves it — a section deleted from
    `suggest_video_tool`'s `require` tuple — is only fatal while something builds the tool at
    module scope, and the line that does that (`SUGGEST_VIDEO_TOOL_NAMES` reading the name back off
    the surface) can be replaced by the literal `SUGGEST_VIDEO_TOOL` with **no behavioural
    difference at all**. Every test in this module passed under that substitution: the two strings
    are equal, the parser accepts the same name, and the only thing lost is the import-time build
    that makes the guard bite. That is a guard whose own mechanism nothing was watching.

    So the module's source is executed with a section removed from the promotion, in a namespace
    that resolves its relative imports, and the failure is required to happen during execution of
    the module rather than at the first call of a function.
    """
    import sys
    import types

    from music_video_producer import director as shipped

    source = pathlib.Path(shipped.__file__).read_text(encoding="utf-8")
    broken = source.replace(
        "require=tuple(BRIEF_SECTIONS)", "require=tuple(BRIEF_SECTIONS)[1:]", 1
    )
    assert broken != source, "suggest_video_tool no longer promotes the mapping"

    # A real module in `sys.modules`, not a bare dict: `from __future__ import annotations` makes
    # every annotation a string, and Pydantic resolves them by looking the module up by name. A
    # dict namespace fails on `AskDirectorArguments` long before this slice's code runs, which
    # would be a green test failing for the wrong reason.
    name = "music_video_producer._director_with_a_dropped_section"
    module = types.ModuleType(name)
    module.__package__ = "music_video_producer"
    module.__file__ = shipped.__file__
    sys.modules[name] = module
    try:
        with pytest.raises(ValueError, match="premise"):
            # `exec` is suppressed below rather than avoided: executing a module's own source is
            # the only way to assert *when* it fails. Importing a mutated copy from a temp file
            # would not resolve the package's relative imports, and calling
            # `suggest_video_tool()` directly would assert the wrong thing — that it raises at
            # all, which it does either way.
            exec(compile(broken, shipped.__file__, "exec"), module.__dict__)  # noqa: S102
    finally:
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------------------------
# The pure pieces: what is thin, and what a suggestion reads as
# ---------------------------------------------------------------------------------------------


def test_a_complete_suggestion_composes_all_five_sections_and_reports_no_shortfall():
    written = compose_brief(suggestion())
    assert brief_shortfall(suggestion()) == ()
    for field, label in BRIEF_SECTIONS.items():
        assert f"## {label}\n{FULL[field]}" in written
    # Reading order, which is the order the Director's eye goes down the box.
    assert [line for line in written.splitlines() if line.startswith("## ")] == [
        f"## {label}" for label in BRIEF_SECTIONS.values()
    ]


def test_a_blank_section_is_a_shortfall_and_is_left_out_of_the_document_entirely():
    """The two halves of "thin", and why the heading does not survive.

    An empty `## Cast` claims the pass considered the cast and found nothing to say. That is a
    different statement from the honest one, and the Director would have to compare five headings
    against a notice to find out which it was.
    """
    thin = suggestion(cast="   ", look="")
    assert brief_shortfall(thin) == ("Cast", "Look")
    written = compose_brief(thin)
    assert "## Cast" not in written
    assert "## Look" not in written
    assert "## Premise" in written
    # And the sections that did arrive are stripped, so the model does not set the spacing.
    assert compose_brief(suggestion(premise="  padded  ")).startswith("## Premise\npadded\n\n")


def test_the_shortfall_is_blankness_and_never_a_judgement_about_length():
    """One word is a section the model wrote. Refusing it would refuse work the Director asked for.

    The line between *thin* and *bad* belongs to the Director reading the brief, and an application
    that drew it would be re-rolling a usable answer on a model measured at 26x variance.
    """
    assert brief_shortfall(suggestion(cast="One.")) == ()
    assert brief_shortfall(suggestion(cast="\n\t ")) == ("Cast",)


# ---------------------------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------------------------


def test_a_well_formed_reply_parses_whether_the_arguments_are_a_string_or_an_object():
    """Both shapes a local model actually produces on this wire."""
    assert parse_suggested_brief(reply(call(**FULL))).premise == FULL["premise"]
    decoded = {
        "type": "function",
        "function": {"name": SUGGEST_VIDEO_TOOL, "arguments": dict(FULL)},
    }
    assert parse_suggested_brief(reply(decoded)).look == FULL["look"]


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(reply(), id="no tool call at all — the model chatted"),
        pytest.param(
            reply(content="Here is what I think."), id="prose and nothing else"
        ),
        pytest.param(
            reply({"type": "function", "function": {"name": "write_brief", "arguments": "{}"}}),
            id="a tool from the other surface",
        ),
        pytest.param(
            reply(
                {
                    "type": "function",
                    "function": {"name": SUGGEST_VIDEO_TOOL, "arguments": "{not json"},
                }
            ),
            id="arguments that are not JSON",
        ),
        pytest.param(
            reply(
                {
                    "type": "function",
                    "function": {"name": SUGGEST_VIDEO_TOOL, "arguments": "[]"},
                }
            ),
            id="arguments that are not an object",
        ),
        pytest.param(
            reply(call(**{key: value for key, value in FULL.items() if key != "arc"})),
            id="a required field the model dropped",
        ),
    ],
)
def test_a_reply_that_carries_no_usable_call_is_refused_by_name(bad):
    """Every one of these is a shape this model produces, and none of them may parse.

    A dropped field is the sharpest: it is the failure `_strict_tool_schema` exists to make
    visible, and it must be a refusal that re-rolls rather than a four-fifths brief. That is the
    opposite disposition from a *blank* field, which is the next test.
    """
    with pytest.raises(DirectorError, match="no usable suggest_video call"):
        parse_suggested_brief(bad)


def test_a_reply_with_every_section_blank_is_refused_rather_than_written():
    """The one shape that validates and still cannot be written.

    Composing it yields the empty string, and writing that would blank a Brief the Director may
    have spent an hour on — the exact loss the recovery slot exists to prevent, caused by the
    writer rather than survived by it. Refused here, so it becomes a retry.
    """
    with pytest.raises(DirectorError, match="filled none of the five sections"):
        parse_suggested_brief(call_reply_all_blank())


def call_reply_all_blank() -> dict:
    return reply(call(**{field: "  " for field in BRIEF_SECTIONS}))


def test_a_reply_with_one_section_blank_parses_so_that_it_can_be_reported_as_partial():
    """A blank field is not a dropped field, and collapsing them costs in both directions.

    `WriteBriefArguments` carries `min_length=1` and this does not, deliberately: there an empty
    document and no document mean the same thing to a route that would write nothing either way,
    and here AD-39 requires the thin reply to be *stored and reported as partial*. With
    `min_length` it would be a `ValidationError` — indistinguishable from a dropped key — and a
    brief that is four-fifths written would be discarded and re-rolled.
    """
    parsed = parse_suggested_brief(reply(call(**{**FULL, "cast": ""})))
    assert brief_shortfall(parsed) == ("Cast",)


def test_a_second_call_in_one_reply_is_read_as_the_first_rather_than_refused():
    """The opposite of `parse_planning_reply`'s rule, and the arguments differ.

    There, two `write_brief` calls are two whole documents with no sane merge and a silent discard
    to avoid. Here the alternative to taking one is taking none, and a model that answered twice
    has answered.
    """
    parsed = parse_suggested_brief(reply(call(**FULL), call(**{**FULL, "premise": "second"})))
    assert parsed.premise == FULL["premise"]


# ---------------------------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------------------------


def transport_client(handler) -> DirectorClient:
    return DirectorClient(
        base_url="http://llm.test/v1",
        model="local-model",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.anyio
async def test_the_call_forces_the_one_tool_and_sends_the_long_pass_persona():
    """`tool_choice` is forced here and `"auto"` on both chat surfaces, which is not an oversight.

    Those two argue against forcing and are right for themselves: forcing makes the model pick one
    of several acts on a turn where prose is honest. Neither argument survives here — there is one
    act, prose is never the honest answer to *write me a brief*, and a chatty roll is a wasted
    pass the Director watches.
    """
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": reply(call(**FULL))}]})

    client = transport_client(handler)
    result = await client.suggest_video(brief_input={"song": {"title": "Signal Bloom"}})
    await client.close()

    assert result.premise == FULL["premise"]
    body = sent[0]
    assert body["tool_choice"] == {"type": "function", "function": {"name": SUGGEST_VIDEO_TOOL}}
    assert [tool["function"]["name"] for tool in body["tools"]] == [SUGGEST_VIDEO_TOOL]
    assert body["messages"][0]["content"] == SUGGEST_VIDEO_SYSTEM_PROMPT
    assert json.loads(body["messages"][1]["content"]) == {"song": {"title": "Signal Bloom"}}


@pytest.mark.anyio
async def test_a_read_timeout_is_reported_by_class_and_never_as_a_blank():
    """`httpx.ReadTimeout` stringifies to `""`. This is the whole reason `failure_description` exists.

    A report built from `str(error)` alone once produced the message "invalid response: ." about
    what was actually a timeout — a blank that reads to the Director as a fault in the application
    rather than as a slow model.
    """
    assert str(httpx.ReadTimeout("")) == ""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    client = transport_client(handler)
    with pytest.raises(DirectorError) as raised:
        await client.suggest_video(brief_input={})
    await client.close()
    assert "ReadTimeout" in str(raised.value)
    assert failure_description(raised.value).startswith("DirectorError: ")
    assert "ReadTimeout" in failure_description(raised.value)


@pytest.mark.anyio
async def test_an_unconfigured_provider_is_unavailable_rather_than_an_error():
    client = DirectorClient(base_url="", model="")
    with pytest.raises(DirectorUnavailable):
        await client.suggest_video(brief_input={})
    await client.close()


# ---------------------------------------------------------------------------------------------
# The retry, which is the requirement
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_first_attempt_that_succeeds_is_not_retried():
    director = SuggestingDirector(suggestion())
    outcome = await run_suggest_video(director, brief_input={})
    assert director.calls == 1
    assert outcome.attempts == 1
    assert outcome.failure == ""


@pytest.mark.anyio
async def test_a_failed_pass_retries_exactly_once_and_not_zero_or_two_times():
    """**The number is the requirement**, and it is asserted as a number rather than as "it retried".

    Zero leaves the Director on the wrong side of a distribution with 26x variance. More than one
    turns a pass measured past 300 seconds into one that cannot be waited out. Both mutations are
    single-character edits to `SUGGEST_VIDEO_ATTEMPTS` and both fail here.
    """
    director = SuggestingDirector(DirectorError("first"), DirectorError("second"))
    outcome = await run_suggest_video(director, brief_input={})
    assert director.calls == 2
    assert SUGGEST_VIDEO_ATTEMPTS == 2
    assert outcome.attempts == 2
    assert outcome.suggestion is None


@pytest.mark.anyio
async def test_the_second_roll_is_a_different_roll_and_recovers_the_pass():
    """The reason the retry is worth having: sampling is independent and the variance is 26x."""
    director = SuggestingDirector(DirectorError("timed out"), suggestion())
    outcome = await run_suggest_video(director, brief_input={})
    assert director.calls == 2
    assert outcome.attempts == 2
    assert outcome.suggestion is not None
    # The failure of the first roll is not reported on a pass that then succeeded.
    assert outcome.failure == ""


@pytest.mark.anyio
async def test_an_unavailable_provider_is_not_retried_because_it_would_fail_identically():
    """The distinction the whole retry rests on: a re-roll only buys anything if it is a re-roll.

    Sampling is independent across calls, so a malformed reply or a timeout is worth another go.
    An unconfigured provider is a settled fact, and a second attempt spends the Director's wait on
    no new information.
    """
    director = SuggestingDirector(DirectorUnavailable("not configured"))
    with pytest.raises(DirectorUnavailable):
        await run_suggest_video(director, brief_input={})
    assert director.calls == 1


@pytest.mark.anyio
async def test_the_reported_failure_names_the_exception_class_and_the_elapsed_time():
    """AD-39: by class and elapsed time, never by the string.

    The clock is injected so the elapsed figure is *stated* rather than merely present. A report of
    elapsed time that no test pins is a report that can quietly become zero.
    """
    ticks = iter([100.0, 412.5])
    director = SuggestingDirector(
        DirectorError("first"), DirectorError("LLM director returned no usable brief: ReadTimeout: ")
    )
    outcome = await run_suggest_video(director, brief_input={}, clock=lambda: next(ticks))
    assert outcome.elapsed == pytest.approx(312.5)
    assert outcome.failure.startswith("DirectorError: ")
    assert "ReadTimeout" in outcome.failure


@pytest.mark.anyio
async def test_the_pass_never_touches_a_project_at_all():
    """Byte-identical is structural here rather than a branch somebody has to keep correct.

    `run_suggest_video` takes a builder's dict and a director. It has no `Project` to write to and
    no store to write through, so a failed pass has no path to the manifest — which is a stronger
    statement than "the failure branch does not save".
    """
    import inspect

    # The body, past its own docstring: the prose above it names a `Project` in order to say that
    # this function never sees one, and a grep that read the explanation rather than the code
    # would be measuring itself. `package_source` strips prose for the same reason.
    body = inspect.getsource(run_suggest_video).split('"""')[2]
    assert "store" not in body
    assert "Project" not in body
    assert "project" not in inspect.signature(run_suggest_video).parameters


# ---------------------------------------------------------------------------------------------
# The extracted capture, which four writers now share
# ---------------------------------------------------------------------------------------------


def test_a_machine_write_captures_for_every_document_and_a_save_only_for_the_brief():
    """The partition `write_document` holds, both arms, at both kinds of writer.

    *Whichever writer is the threat fills the slot.* What destroys a Treatment is a model rewrite,
    so a save must not spend its slot; what destroys a Brief is a save over pasted text, and no
    reply can write one, so its own save is the displacement there is.
    """
    project = Project(name="Partition")
    project.treatment = "The treatment the Director wrote by hand."
    project.creative_brief = "The brief the Director wrote by hand."

    assert write_document(project, "treatment", "typed over it", writer=DOCUMENT_WRITER_SAVE) is False
    assert project.treatment == "typed over it"
    assert project.treatment_previous == ""

    assert write_document(project, "creative_brief", "typed over it", writer=DOCUMENT_WRITER_SAVE)
    assert project.creative_brief_previous == "The brief the Director wrote by hand."

    assert write_document(project, "treatment", "a model wrote this", writer=DOCUMENT_WRITER_MACHINE)
    assert project.treatment_previous == "typed over it"
    assert write_document(
        project, "creative_brief", "a model wrote this", writer=DOCUMENT_WRITER_MACHINE
    )
    assert project.creative_brief_previous == "typed over it"
    # And the partition it reads is the derived one, not a list written out here.
    assert SAVE_CAPTURED_DOCUMENTS == ("creative_brief",)


def test_a_byte_equal_write_captures_nothing_whichever_writer_makes_it():
    """The one case where doing nothing is the whole feature.

    The copy the slot would overwrite is the recoverable one and the copy going in is the live one,
    so an echo would annihilate the genuinely recoverable version with a duplicate of the live one.
    """
    project = Project(name="Echo")
    project.creative_brief = "Unchanged."
    project.creative_brief_previous = "The version worth keeping."
    for writer in (DOCUMENT_WRITER_SAVE, DOCUMENT_WRITER_MACHINE):
        assert write_document(project, "creative_brief", "Unchanged.", writer=writer) is False
        assert project.creative_brief_previous == "The version worth keeping."


def test_every_writer_of_a_creative_document_goes_through_the_one_capture():
    """Constraint 2, asserted against the source: no fifth inline copy of the displacement.

    The capture was written out three times — `replace_documents`, `director_chat`'s apply loop and
    `planning_turn` — with the site carrying a comment saying **extract before duplicating**, and
    Suggest Video would have been the fourth. `write_document` is the one implementation, so the
    only assignment to a `*_previous` field anywhere in the package is the restore's own swap,
    which is a different act: it *reads* the slot the writers fill.
    """
    import ast

    from package_source import module_name, package_modules

    def enclosing(node, parents, fallback):
        """The name of the function a node sits inside, or the module's own name."""
        while node is not None:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return node.name
            node = parents.get(node)
        return f"<{fallback}>"

    writers = set()
    for path in package_modules():
        # The **raw** source rather than `module_code`: a docstring cannot hold an assignment
        # node, so prose is excluded by the parse itself, and `module_code` blanks docstrings in
        # place, which leaves a docstring-only class body unparseable.
        code = path.read_text(encoding="utf-8")
        tree = ast.parse(code)
        parents = {
            child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
        }

        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            slot = any(
                isinstance(target, ast.Attribute) and target.attr.endswith(RECOVERY_SLOT_SUFFIX)
                for target in targets
            )
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setattr":
                segment = ast.get_source_segment(code, node) or ""
                slot = slot or "RECOVERY_SLOT_SUFFIX" in segment or "_previous" in segment
            if slot:
                writers.add(enclosing(node, parents, module_name(path)))

    assert writers == {
        # The one displacement, shared by the Director's save, the chat route's apply loop, the
        # planning turn and Suggest Video.
        "write_document",
        # The swap, which *reads* the slot the writers fill and is the opposite act.
        "restore_document",
        # The Song's own two context fields, which are not creative documents and have their own
        # single writer (`replace_song_context`'s own note says a lyric sheet has exactly one).
        "replace_song_context",
        # `replace_project` taking the stored slots off a client body, which writes no version.
        "_detach_song_recovery_slots",
    }, writers


# ---------------------------------------------------------------------------------------------
# The input the pass is given
# ---------------------------------------------------------------------------------------------


def test_the_input_carries_the_song_and_never_the_documents_derived_from_the_brief(tmp_path):
    """What is absent is the argument: the Treatment and the Style bible are made *from* the Brief.

    Feeding them back would ask the pass to reverse the dependency it exists at the start of, and
    it cannot write either of them in any case (TP-10).
    """
    _client, store, _ = make_client(tmp_path)
    project = suggestable_project(store, brief="Three lines the Director typed first.")
    project.treatment = "A treatment that must not reach this pass."
    project.style_bible = "A style bible that must not reach it either."
    project.song.vocal_type = "female"
    store.save(project)

    built = suggest_video_input(store.get(project.id))
    assert set(built) == {"song", "sections", "existing_brief"}
    assert built["song"] == {
        "title": "Signal Bloom",
        "duration_seconds": 202.0,
        "lyrics": LYRICS,
        "style": STYLE,
        "vocal_type": "female",
    }
    assert built["existing_brief"] == "Three lines the Director typed first."
    assert "treatment" not in json.dumps(built)
    assert "style bible" not in json.dumps(built).lower()


def test_sections_are_used_when_present_and_are_an_empty_list_when_they_are_not(tmp_path):
    """R-15 in the builder: a sectioned song gets a better pass, an unsectioned one still runs.

    Nothing here infers a structure, because a structure this pass invented would be
    indistinguishable downstream from one the Director marked by ear.
    """
    _client, store, _ = make_client(tmp_path)
    project = suggestable_project(store)
    assert suggest_video_input(project)["sections"] == []

    project.sections = [
        SongSection(label="Chorus", start=40.0, duration=20.0, prompt="on the bed"),
        SongSection(label="Verse", start=8.0, duration=32.0),
    ]
    built = suggest_video_input(store.save(project))
    # Sorted by start, whatever order they are stored in: the generic project PUT writes this
    # field and does not have to sort it.
    assert [section["label"] for section in built["sections"]] == ["Verse", "Chorus"]
    assert built["sections"][1] == {
        "label": "Chorus",
        "start": 40.0,
        "duration": 20.0,
        "prompt": "on the bed",
    }


def test_the_builder_is_total_on_a_project_with_no_song():
    """A pure builder that can raise is a second place a caller has to remember to guard.

    The refusal is `suggest_video_refusal`'s, before any of this is built.
    """
    assert suggest_video_input(Project(name="Empty"))["song"]["title"] == ""


# ---------------------------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------------------------


def test_an_incomplete_song_refuses_by_name_and_says_where_to_fill_it(tmp_path):
    """TP-3 and UX-TP12. A generic failure is the worst version of this message.

    The Director pressed a button that takes minutes when it works; "could not run" tells them
    nothing about which of two boxes on another page is empty.
    """
    client, store, _ = make_client(tmp_path, director=SuggestingDirector())
    project = suggestable_project(store, lyrics="", caption="")

    response = client.post(SUGGEST.format(project=project.id))

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "Lyric sheet" in detail
    assert "Style description" in detail
    assert "Song page" in detail
    # R-7: it does not care how the details arrived, and the refusal says so.
    assert "imported" in detail


def test_the_refusal_names_only_the_field_that_is_actually_missing(tmp_path):
    _client, store, _ = make_client(tmp_path)
    project = suggestable_project(store, caption="")
    detail = suggest_video_refusal(store.get(project.id))
    assert "Style description" in detail
    assert "Lyric sheet" not in detail


def test_a_project_with_no_song_refuses_before_anything_else(tmp_path):
    client, store, _ = make_client(tmp_path, director=SuggestingDirector())
    project = store.create(Project(name="No song"))

    response = client.post(SUGGEST.format(project=project.id))

    assert response.status_code == 422, response.text
    assert "master song" in response.json()["detail"]


def test_an_incomplete_song_is_refused_without_the_model_being_called(tmp_path):
    """The refusal is decided from stored state, so spending minutes to reach it would be a bug."""
    director = SuggestingDirector()
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store, lyrics="")

    assert client.post(SUGGEST.format(project=project.id)).status_code == 422
    assert director.calls == 0


def test_neither_sections_nor_a_song_analysis_are_a_precondition(tmp_path):
    """R-15 and TP-5 at the route: the precondition is a Song record and nothing more.

    Requiring structure would make the song analysis a prerequisite for the first stage of
    planning, which is the opposite of what TP-5 asks for.
    """
    director = SuggestingDirector()
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store)
    assert project.sections == []
    assert project.song.analysis.path == ""

    response = client.post(SUGGEST.format(project=project.id))

    assert response.status_code == 200, response.text
    assert director.inputs[0]["sections"] == []
    assert store.get(project.id).creative_brief.startswith("## Premise")


def test_a_locked_brief_refuses_the_write_by_name_and_never_calls_the_model(tmp_path):
    """Story 12.1's lock, asked through `document_lock_refusal` rather than reimplemented.

    Same question, same answer, same sentence as the one that refuses a Director reply — which is
    the whole reason that function was extracted. And asked *before* the call, because the
    alternative is spending minutes of the Director's time to answer a settled question.
    """
    director = SuggestingDirector()
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store, brief="An hour of revising.")
    project.creative_brief_locked = True
    store.save(project)
    before = manifest(store, project.id)

    response = client.post(SUGGEST.format(project=project.id))

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == DOCUMENT_LOCK_NOTICE.format(
        document=DOCUMENT_LABELS["creative_brief"]
    )
    assert director.calls == 0
    assert manifest(store, project.id) == before


def test_a_lock_set_while_the_pass_runs_still_refuses_the_write(tmp_path):
    """A long pass holds nothing open, and a Director who locked the Brief has said something newer.

    `director_chat` re-reads after its await for the same reason; this is the same window, only
    longer — this project has measured a pass past 300 seconds.
    """
    store_box: dict = {}

    class LockingDirector(SuggestingDirector):
        async def suggest_video(self, *, brief_input):
            project = store_box["store"].get(store_box["id"])
            project.creative_brief_locked = True
            store_box["store"].save(project)
            return await super().suggest_video(brief_input=brief_input)

    director = LockingDirector(suggestion())
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store, brief="An hour of revising.")
    store_box.update(store=store, id=project.id)

    response = client.post(SUGGEST.format(project=project.id))

    assert response.status_code == 409, response.text
    assert director.calls == 1
    stored = store.get(project.id)
    assert stored.creative_brief == "An hour of revising."
    assert stored.creative_brief_previous == ""


# ---------------------------------------------------------------------------------------------
# The route: what it writes, and what it does not
# ---------------------------------------------------------------------------------------------


def test_a_pass_writes_a_brief_covering_all_five_and_sends_the_existing_one_to_the_slot(tmp_path):
    """The acceptance criterion, end to end, plus story 12.1's displacement.

    Premise, cast, locations, arc and look — the minimum TP-3 names — and the text that was there
    goes to the recovery slot rather than being overwritten.
    """
    director = SuggestingDirector(suggestion())
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store, brief="Three lines the Director typed first.")

    response = client.post(SUGGEST.format(project=project.id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["partial"] is False
    assert body["missing"] == []
    assert body["restorable"] is True
    assert body["attempts"] == 1
    stored = store.get(project.id)
    assert stored.creative_brief == compose_brief(suggestion())
    for label in BRIEF_SECTIONS.values():
        assert f"## {label}" in stored.creative_brief, label
    assert stored.creative_brief_previous == "Three lines the Director typed first."
    assert body["project"]["creative_brief"] == stored.creative_brief
    assert "can be restored" in body["notice"]


def test_a_pass_writes_the_brief_and_nothing_else(tmp_path):
    """TP-3 and TP-10, measured on the manifest rather than argued from the tool's schema.

    The schema makes a Treatment unrepresentable; this makes *every other field of the project*
    unchanged, which is the claim a Director actually cares about — no shots, no assets, no
    sections, no song edits, no status moves.
    """
    director = SuggestingDirector(suggestion())
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store, brief="Before.")
    project.treatment = "A treatment written by hand."
    project.style_bible = "A style bible written by hand."
    before = store.save(project).model_dump(mode="json")

    assert client.post(SUGGEST.format(project=project.id)).status_code == 200

    after = store.get(project.id).model_dump(mode="json")
    changed = {key for key in after if before[key] != after[key]}
    assert changed == {"creative_brief", "creative_brief_previous", "updated_at"}


def test_a_first_draft_into_a_blank_brief_reports_that_there_is_nothing_to_restore(tmp_path):
    """A blank target captures an empty slot, and a restore would refuse.

    Describing that as a replacement whose previous version "can be restored" is a promise broken
    by the very next click — `DOCUMENT_FIRST_DRAFT_NOTICE`'s argument, applied to this pass.
    """
    client, store, _ = make_client(tmp_path, director=SuggestingDirector(suggestion()))
    project = suggestable_project(store, brief="")

    body = client.post(SUGGEST.format(project=project.id)).json()

    assert body["restorable"] is False
    assert "no previous version to restore" in body["notice"]
    assert store.get(project.id).creative_brief_previous == ""


def test_a_thin_reply_is_stored_and_reported_as_partial(tmp_path):
    """AD-39: stored, and **never presented as a finished Brief**.

    Four sections out of five is work the Director can react to, which is the whole point of the
    feature. `document_rejection`'s ratio floor is deliberately not applied here for the same
    reason: it would refuse exactly the short-but-real answer this route is required to keep.
    """
    director = SuggestingDirector(suggestion(cast="", look="   "))
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store, brief="Before.")

    response = client.post(SUGGEST.format(project=project.id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["partial"] is True
    assert body["missing"] == ["Cast", "Look"]
    assert body["notice"].startswith("Partial:")
    assert "Cast, Look" in body["notice"]
    # Stored, not refused, and not re-rolled: one attempt.
    assert director.calls == 1
    stored = store.get(project.id)
    assert "## Premise" in stored.creative_brief
    assert "## Cast" not in stored.creative_brief
    assert stored.creative_brief_previous == "Before."


def test_a_second_identical_pass_says_so_instead_of_calling_a_full_brief_blank(tmp_path):
    """**Through the route, because the wiring is what keeps surviving.**

    Two passes, the same suggestion both times. The second composes text byte-identical to what is
    stored, `write_document` captures nothing on that comparison, and `restorable` therefore comes
    back `False` — the same `False` a first draft into a blank Brief produces. Reading the
    sentence off `restorable` alone told a Director looking at a page of their own words that it
    *"was blank"*, which is what this asserts is gone.

    The slot is asserted too: the first pass filled it, and the second must leave it alone rather
    than overwrite the recoverable copy with the live one.
    """
    director = SuggestingDirector(suggestion(), suggestion())
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store, brief="Before.")

    first = client.post(SUGGEST.format(project=project.id)).json()
    second = client.post(SUGGEST.format(project=project.id)).json()

    assert first["restorable"] is True
    assert "can be restored" in first["notice"]
    # The second wrote the same bytes, so there was nothing to displace.
    assert second["restorable"] is False
    assert "returned the same text" in second["notice"]
    assert "blank" not in second["notice"]
    assert "no previous version" not in second["notice"]
    stored = store.get(project.id)
    assert stored.creative_brief == compose_brief(suggestion())
    assert stored.creative_brief_previous == "Before."


def test_a_partial_pass_is_never_worded_as_a_finished_one(tmp_path):
    """The wording rule, isolated from the route so the branch itself is asserted.

    A sentence that led with "wrote the Creative brief" and mentioned the shortfall afterwards
    would be exactly the presentation AD-39 forbids, and it is the natural thing to write.
    """
    thin = SuggestVideoOutcome(suggestion=suggestion(arc=""), attempts=1, elapsed=12.0)
    # **Every combination of the two facts below it**, because partial's primacy is
    # unconditional and `changed` is a second thing that could have unseated it — the
    # unchanged wording names no shortfall, so a pass that returned thin text twice would
    # lose the only actionable part of its report.
    for restorable in (True, False):
        for changed in (True, False):
            notice = suggest_video_notice(thin, restorable=restorable, changed=changed)
            assert notice.startswith("Partial:")
            assert "Arc" in notice
    whole = SuggestVideoOutcome(suggestion=suggestion(), attempts=1, elapsed=12.0)

    def wording(**facts: bool) -> str:
        return suggest_video_notice(whole, **facts)

    assert wording(restorable=True, changed=True).startswith("Suggest Video wrote")
    assert "can be restored" in wording(restorable=True, changed=True)
    assert "no previous version" in wording(restorable=False, changed=True)
    assert "12.0s" in wording(restorable=True, changed=True)
    # The third arm. It claims nothing about the previous state and nothing about a kept
    # version existing, so it is the one wording that is true whatever the slot holds.
    for restorable in (True, False):
        unchanged = wording(restorable=restorable, changed=False)
        assert "returned the same text" in unchanged
        assert "12.0s" in unchanged
        assert "blank" not in unchanged
        assert "no previous version" not in unchanged
        assert "can be restored" not in unchanged


def test_a_final_failure_leaves_the_manifest_byte_identical_and_names_the_class(tmp_path):
    """**The byte-identical guarantee, proved by comparing bytes rather than by reading the code.**

    A timeout after the retry: two calls, a 502, and a manifest that is the same file it was. The
    report names the exception class and the elapsed time — never the exception's string, because
    `httpx.ReadTimeout` stringifies to `""` and a Director would be shown a blank.
    """
    timeout = DirectorError(
        "LLM director returned no usable brief: "
        f"{failure_description(httpx.ReadTimeout(''))}"
    )
    director = SuggestingDirector(timeout, timeout)
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store, brief="An hour of revising.")
    before = manifest(store, project.id)

    response = client.post(SUGGEST.format(project=project.id))

    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    assert "ReadTimeout" in detail
    assert "2 attempt(s)" in detail
    assert "Nothing was written" in detail
    assert DOCUMENT_LABELS["creative_brief"] in detail
    assert director.calls == 2
    assert manifest(store, project.id) == before


def test_a_malformed_reply_is_retried_once_and_then_reported(tmp_path):
    """"Malformed output" is the other half of AD-39's retry trigger, and reaches it the same way.

    The parser turns a reply with no usable call into a `DirectorError`, which is what the loop
    catches — so a chatty roll and a timed-out roll are one retry policy rather than two.
    """
    director = SuggestingDirector(
        DirectorError("the reply carried no usable suggest_video call"),
        DirectorError("the reply carried no usable suggest_video call"),
    )
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store, brief="An hour of revising.")
    before = manifest(store, project.id)

    response = client.post(SUGGEST.format(project=project.id))

    assert response.status_code == 502
    assert "no usable suggest_video call" in response.json()["detail"]
    assert director.calls == 2
    assert manifest(store, project.id) == before


def test_a_pass_that_fails_once_and_then_succeeds_writes_on_the_second_roll(tmp_path):
    """What the retry buys, at the route: one lost roll and a Brief anyway."""
    director = SuggestingDirector(DirectorError("timed out"), suggestion())
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store)

    response = client.post(SUGGEST.format(project=project.id))

    assert response.status_code == 200, response.text
    assert response.json()["attempts"] == 2
    assert director.calls == 2
    assert store.get(project.id).creative_brief.startswith("## Premise")


def test_an_all_blank_reply_is_retried_and_never_blanks_the_brief(tmp_path):
    """The refusal that matters most: a reply that validates and would erase the document.

    It is refused in the parser, so it becomes a retry rather than a write of the empty string —
    the recovery slot's own loss, caused by the writer instead of survived by it.
    """
    empty = DirectorError("the reply filled none of the five sections")
    director = SuggestingDirector(empty, empty)
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store, brief="An hour of revising.")
    before = manifest(store, project.id)

    assert client.post(SUGGEST.format(project=project.id)).status_code == 502
    assert manifest(store, project.id) == before
    assert store.get(project.id).creative_brief == "An hour of revising."


def test_an_unconfigured_director_is_a_503_and_writes_nothing(tmp_path):
    director = SuggestingDirector(DirectorUnavailable("LLM director is not configured."))
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store, brief="Before.")
    before = manifest(store, project.id)

    response = client.post(SUGGEST.format(project=project.id))

    assert response.status_code == 503, response.text
    assert director.calls == 1
    assert manifest(store, project.id) == before


def test_the_pass_sends_the_song_and_the_existing_brief_to_the_model(tmp_path):
    """The payload the model really saw, asserted against the builder's own output.

    A route that built its own dict here would be a second definition of what this pass reads.
    """
    director = SuggestingDirector(suggestion())
    client, store, _ = make_client(tmp_path, director=director)
    project = suggestable_project(store, brief="Three lines the Director typed first.")

    client.post(SUGGEST.format(project=project.id))

    assert director.inputs == [suggest_video_input(store.get(project.id).model_copy(
        update={"creative_brief": "Three lines the Director typed first."}
    ))]


# ---------------------------------------------------------------------------------------------
# TP-5: Suggest Video is a prerequisite for nothing
# ---------------------------------------------------------------------------------------------


def test_a_director_who_never_runs_suggest_video_sees_nothing_refuse_or_degrade(tmp_path):
    """TP-5, asserted rather than assumed (spec constraint 6).

    A Director who typed their Brief by hand and never touched this control uses the rest of the
    feature: they save documents, lock one, restore one, and take a planning turn. Every one of
    them answers exactly as it would have before this slice existed, and nothing anywhere warns
    that a pass was not run.

    **The structural half is the stronger one and comes first**: this slice adds no `Project`
    field, so there is nothing for another route to read and find absent. A feature that stores no
    trace of having run cannot be a prerequisite for anything.
    """
    fields_before = {
        "id", "name", "created_at", "updated_at", "creative_brief", "treatment", "style_bible",
        "creative_brief_previous", "treatment_previous", "style_bible_previous",
        "creative_brief_locked", "treatment_locked", "style_bible_locked", "song", "sections",
    }
    assert fields_before <= set(Project.model_fields)
    assert not {name for name in Project.model_fields if "suggest" in name}

    class QuietDirector(SuggestingDirector):
        async def assist_planning(self, *, message, project_context):
            from music_video_producer.director import PlanningTurn

            return PlanningTurn(message="Noted.", questions=["Who is driving?"])

    client, store, _ = make_client(tmp_path, director=QuietDirector())
    project = suggestable_project(store, brief="")

    typed = "A night drive that opens into wilderness. One driver, one wolf, no dialogue."
    saved = client.put(
        f"/api/projects/{project.id}/documents",
        json={"creative_brief": typed, "treatment": "", "style_bible": ""},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["creative_brief"] == typed

    revised = client.put(
        f"/api/projects/{project.id}/documents",
        json={"creative_brief": typed + " Shot at night.", "treatment": "", "style_bible": ""},
    )
    assert revised.status_code == 200, revised.text
    restored = client.post(f"/api/projects/{project.id}/documents/creative_brief/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["creative_brief"] == typed

    locked = client.put(
        f"/api/projects/{project.id}/documents",
        json={
            "creative_brief": typed,
            "treatment": "",
            "style_bible": "",
            "creative_brief_locked": True,
        },
    )
    assert locked.status_code == 200, locked.text

    turn = client.post(
        f"/api/projects/{project.id}/planning/turn",
        json={"message": "What do you need to know?", "apply_documents": False},
    )
    assert turn.status_code == 200, turn.text
    notices = [
        notice
        for message in turn.json()["messages"]
        if message["role"] == "assistant"
        for notice in message["notices"]
    ]
    assert notices, "the planning turn reported nothing at all"
    assert not any("Suggest Video" in notice["text"] for notice in notices)
    assert not any(notice["kind"] == "refusal" for notice in notices)
    # And the read paths a Director uses are untouched by never having run it.
    assert client.get(f"/api/projects/{project.id}").status_code == 200
    assert client.get(f"/api/projects/{project.id}/readiness").status_code == 200
