import inspect
import json
import re
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import get_args

import pytest
from fastapi import HTTPException

# One definition of "a count of panels", shared with the scan that forbids it in source, so
# the guard the templates are executed against cannot drift from the guard the repo is
# scanned with -- and neither can go inert while the other still passes.
from test_workflows import PANEL_COUNT_PATTERN

from music_video_producer.app import (
    APPLY_DOCUMENTS_LABEL,
    ASSET_NAME_LIMIT,
    CONSISTENCY_PROMPT_LIMIT,
    DOCUMENT_LABELS,
    DOCUMENT_LOCK_NOTICE,
    DOCUMENT_RESTORE_REFUSAL,
    MULTIVIEW_SUBJECTS,
    SECTION_LOOK_SKIP_ALL_WRITTEN,
    SECTION_LOOK_SKIP_WRITTEN,
    SECTION_LOOKS_ALL_WRITTEN,
    SONG_CAPTION_LIMIT,
    SONG_CONTEXT_LABELS,
    SONG_CONTEXT_RESTORE_NOTICE,
    SONG_CONTEXT_RESTORE_REFUSAL,
    SONG_LYRICS_LIMIT,
    SONG_REPLACEMENT_CONSEQUENCE,
    MusicRequest,
    SnapCutsRequest,
    SongPlannerRequest,
    _require_song_replacement_confirmation,
    create_app,
    document_not_requested_notice,
    document_restore_notice,
)
from music_video_producer.batch import (
    TERMINAL_JOB_STATUSES,
    format_duration,
    reconcilable_jobs,
    render_timing_summary,
)
from music_video_producer.models import (
    ASSET_ROLE_LABELS,
    CHARACTER_SLOT_LIMIT,
    INSTRUMENTAL_NOTE,
    LEGACY_SHOT_MODES,
    SHOT_MODE_SPECS,
    SHOT_PLAN_CONTENT_FIELDS,
    SHOT_TAKE_PROVENANCE_FIELDS,
    SHOT_UNINHERITED_DECISION_FIELDS,
    VOCAL_TYPE_SPECS,
    Asset,
    AssetCitation,
    AssetKind,
    MessageNotice,
    Project,
    RenderJob,
    Shot,
    SingingState,
    Song,
    TreatmentMessage,
    dangling_citations,
    mode_specification_problems,
    resolve_shot_mode,
)
from music_video_producer.timeline import (
    SNAP_TOLERANCE_DEFAULT,
    SNAP_TOLERANCE_MAX,
    lyric_line_tags,
    tag_lyric_line,
)
from music_video_producer.workflows import MUSIC3_MAX_DURATION_SECONDS

APP_JS = Path("src/music_video_producer/web/assets/app.js")
# `shotWriteInFlight` released in a `finally`, whether the block is one line or many. It was
# pinned as a literal one-liner, which made "the flag is cleared even when the call throws" --
# the property that actually matters -- indistinguishable from "the clearing is on one line".
# Reformatting the whole-plan sweep's `finally` to fit a second control broke the assertion
# without touching the guarantee.
RELEASED_IN_FINALLY = re.compile(r'finally \{\s*shotWriteInFlight = "";')
API_JS = Path("src/music_video_producer/web/assets/api.js")
INDEX_HTML = Path("src/music_video_producer/web/index.html")
STYLES_CSS = Path("src/music_video_producer/web/assets/styles.css")

# Every preset the markup actually offers, and the endpoint it must resolve to.
# A typo in either the markup or the mapping must not silently route a cover
# request to the direct Music 3 adapter.
PRESET_ENDPOINTS = {
    "balanced": "music",
    "songplanner-invented": "songplanner",
    "songplanner-known": "songplanner",
}


def markup_preset_values() -> list[str]:
    """The `<option>` values of the Song workspace preset select, read from the markup."""
    select = re.search(
        r'<select name="preset".*?</select>', INDEX_HTML.read_text(encoding="utf-8"), re.DOTALL
    )
    assert select, "the Song workspace form no longer has a preset select"
    return re.findall(r'<option value="([^"]+)"', select.group(0))


def app_js_block(anchor: str, terminator: str = "\n}") -> str:
    """The source of one app.js function or handler, for the DOM code no import can reach."""
    source = APP_JS.read_text(encoding="utf-8")
    assert anchor in source, anchor
    return source.split(anchor, 1)[1].split(terminator, 1)[0]


def without_comments(source: str) -> str:
    """`source` with its `//` comment lines dropped, so assertions are about code only."""
    return "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))


def scoped_control_group(document_tab: str) -> str:
    """The markup of the controls group scoped to one document tab."""
    group = re.search(
        rf'<div class="document-scoped" data-doc-controls="{document_tab}">.*?</div>',
        INDEX_HTML.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert group, f"no per-document controls group is scoped to the {document_tab} tab"
    return group.group(0)


def run_module(script: str):
    # Decoded as UTF-8 explicitly. `text=True` alone decodes node's stdout with the *platform*
    # encoding, which on Windows is a code page that mangles every typographic character the
    # workspace draws — the ellipsis in "Attach asset…", the em dashes in the mode select — into
    # replacement characters, so an assertion about a string the Director really sees fails for a
    # reason that has nothing to do with the code under test. `batch.READINESS_REFUSAL` is
    # deliberately ASCII to dodge exactly this; that is the right call for a string two languages
    # must agree on character for character, and the wrong one to force on the whole interface.
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


# A stub DOM barely complete enough to boot `app.js` under node, so the workspace's own code can be
# *run* rather than read.
#
# Everything else in this file that concerns app.js reads its source, because a browser is not
# available -- and source reading has one hole this closes: a function can be perfectly correct, and
# asserted about in detail, while nothing ever calls it. `renderSongContext` is the case that
# matters. It is the only thing that seeds `#song-lyrics`/`#song-style` from the stored Song and the
# only thing that clears the `disabled` state the markup ships on both boxes and the save button; it
# is reached from exactly one line inside `renderSong`. Delete that line and the entire
# edit-after-import path is dead in every project, with every string assertion in this file still
# passing. So the render is executed here and the boxes are read afterwards.
#
# The stub creates an element for any selector asked for, records every listener bound to it, and
# records every request `fetch` is handed before rejecting it -- which is what makes "the guard
# stopped the switch" and "the save was never sent" observable. `window.confirm` throws until a test
# answers it, so an unexpected question is a failure rather than a silently accepted one.
WORKSPACE_HARNESS = """
const registry = new Map();
const listeners = new Map();
const requests = [];
const make = (selector) => ({
  selector, value: "", disabled: false, checked: false, textContent: "", innerHTML: "",
  className: "", title: "", src: "", min: "", max: "", required: false,
  dataset: {}, style: {}, files: [],
  elements: new Proxy({}, { get: (bag, name) => (bag[name] ||= make(selector + "[" + String(name) + "]")) }),
  classList: {
    flags: new Set(),
    add(name) { this.flags.add(name); },
    remove(name) { this.flags.delete(name); },
    toggle(name, on) { if (on) this.flags.add(name); else this.flags.delete(name); },
    contains(name) { return this.flags.has(name); },
  },
  addEventListener(type, handler) { listeners.set(selector + ":" + type, handler); },
  append() {}, remove() {}, pause() {}, load() {}, click() {},
  setAttribute() {}, removeAttribute() {}, getAttribute() { return null; },
  querySelectorAll: () => [],
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 300 }),
  closest() { return this; },
  // A minimal scoped `querySelector`, over the markup this element was just given. Id
  // selectors only, which is every scoped single-element lookup in app.js. It answered `null`
  // before, and that was the same hole `querySelectorAll` closed below: a control drawn into a
  // bar and then bound with `$("#id", bar)` was bound to nothing in this harness, so no test
  // could click one -- which is exactly how a two-stage report-then-confirm button would go
  // untested. Each match resolves to the registry's own element, so a test fires it by id.
  querySelector(selector) {
    if (!selector.startsWith("#")) return null;
    return String(this.innerHTML || "").includes('id="' + selector.slice(1) + '"')
      ? at(selector)
      : null;
  },
  // A minimal scoped `querySelectorAll`, over the markup this element was just given. Class
  // selectors only, which is every scoped use in app.js.
  //
  // It answered `[]` before, and that was a hole rather than a simplification: a handler bound with
  // `$$(".remove-ref", inspector)` was bound to nothing in this harness, so no test could reach one.
  // Per-citation controls are the case that made it matter -- a role select drawn once per cited
  // asset cannot be addressed by a fixed id, so the only way to execute one is to find it the way
  // the panel's own code does.
  //
  // Each match is registered under `<selector>[<data-id>]`, so a test fires it as
  // `.citation-role[asset_wolf]:change` and sets `.value` on the same element the handler reads.
  querySelectorAll(selector) {
    if (!selector.startsWith(".")) return [];
    const wanted = selector.slice(1);
    const found = [];
    for (const match of String(this.innerHTML || "").matchAll(/<(\\w+)\\s([^>]*?)>/g)) {
      const attributes = match[2];
      const classes = ((/class="([^"]*)"/.exec(attributes) || [null, ""])[1]).split(/\\s+/);
      if (!classes.includes(wanted)) continue;
      const id = (/data-id="([^"]*)"/.exec(attributes) || [null, ""])[1];
      const element = at(selector + "[" + id + "]");
      element.dataset.id = id;
      found.push(element);
    }
    return found;
  },
});
const at = (selector) => {
  if (!registry.has(selector)) registry.set(selector, make(selector));
  return registry.get(selector);
};
// The generation form is read at bind time by syncMusicVariant, which needs a real preset.
at("#music-form").elements.preset.value = "balanced";
globalThis.document = { querySelector: at, querySelectorAll: () => [], createElement: () => make("<created>") };
globalThis.window = {
  addEventListener(type, handler) { listeners.set("window:" + type, handler); },
  confirm: (question) => { throw new Error("unanswered confirm: " + question); },
  prompt: () => null,
};
// Canned answers keyed by exact path, for the tests that need the workspace to *receive*
// something rather than only to be watched sending. `__RESPONSES__` is `{}` for every existing
// caller, so an unlisted path rejects exactly as it always did and nothing already written here
// changes behaviour. Each entry is `{body}` for a 200, or `{status, body}` to be refused.
const responses = new Map(Object.entries(__RESPONSES__));
globalThis.fetch = (path, options = {}) => {
  requests.push({ path, method: options.method || "GET", body: options.body || null });
  if (!responses.has(path)) return Promise.reject(new Error("the contract harness makes no requests"));
  const canned = responses.get(path);
  const status = canned.status || 200;
  return Promise.resolve({
    ok: status < 400,
    status,
    statusText: "canned",
    headers: { get: () => "application/json" },
    json: async () => canned.body,
  });
};
// The workspace boots on import: it binds every handler, then fires its startup requests, which
// reject. Timers are stubbed out so a pending toast cannot hold the process open for its 4.2 s.
globalThis.setTimeout = () => 0;
globalThis.setInterval = () => 0;
globalThis.requestAnimationFrame = () => 0;
const app = await import('./src/music_video_producer/web/assets/app.js');
const contract = await import('./src/music_video_producer/web/assets/api.js');
const { state } = await import('./src/music_video_producer/web/assets/state.js');
const fire = (key, event = {}) => {
  const handler = listeners.get(key);
  if (!handler) throw new Error("nothing is bound to " + key);
  return handler(event);
};
let asked = [];
const answer = (reply) => {
  asked = [];
  requests.length = 0;
  globalThis.window.confirm = (question) => { asked.push(question); return reply; };
};
// The workspace's startup requests are fired but not awaited by the module's top level, so a test
// that asserts about what a *reply* did to the DOM has to let those promises settle first. Real
// promises, so draining the microtask queue is enough -- and `setTimeout` is stubbed out, so
// waiting on a timer would hang instead.
const flush = async () => { for (let index = 0; index < 60; index += 1) await Promise.resolve(); };
"""


def run_workspace(body: str, responses: dict | None = None):
    """Boot `app.js` against the stub DOM and run `body` against the workspace it produced.

    `responses` maps an exact request path to `{"body": ...}` for a 200, or
    `{"status": N, "body": ...}` for a refusal. Anything unlisted is rejected, which is what
    every caller that passes nothing gets.
    """
    harness = WORKSPACE_HARNESS.replace("__RESPONSES__", json.dumps(responses or {}))
    return run_module(harness + body)


def document_controls() -> dict:
    """api.js's one table of per-document selectors, project fields and document tab."""
    return run_module("""
      import { DOCUMENT_CONTROLS } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify(DOCUMENT_CONTROLS));
    """)


def document_opt_in() -> dict:
    """api.js's half of the per-turn document consent: the control, its label, its toast."""
    return run_module("""
      import { APPLY_DOCUMENTS_CONTROL, APPLY_DOCUMENTS_LABEL, DOCUMENT_NOT_APPLIED_TOAST }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        control: APPLY_DOCUMENTS_CONTROL,
        label: APPLY_DOCUMENTS_LABEL,
        toast: DOCUMENT_NOT_APPLIED_TOAST,
      }));
    """)


def chat_submit_handler() -> str:
    """The source of the chat composer's submit handler, which no import can reach."""
    return APP_JS.read_text(encoding="utf-8").split(
        '$("#chat-form").addEventListener("submit"', 1
    )[1].split("  });", 1)[0]


def test_async_project_submit_keeps_stable_form_reference():
    source = APP_JS.read_text(encoding="utf-8")
    handler = source.split('$("#project-form").addEventListener("submit"', 1)[1].split("  });", 1)[0]

    assert "const form = event.currentTarget;" in handler
    assert "form.reset();" in handler
    assert "event.currentTarget.reset()" not in handler


def test_music_preset_selects_endpoint_and_builds_matching_body():
    script = """
      import { musicGenerationPlan } from './src/music_video_producer/web/assets/api.js';
      const invented = musicGenerationPlan({
        preset: 'songplanner-invented', title: 'Night Signal',
        caption: 'sunset synthwave idea', duration: '90', seed: '7',
      });
      const known = musicGenerationPlan({
        preset: 'songplanner-known', title: 'Night Signal (Cover)',
        caption: 'faithful synthwave cover', lyrics: '[verse]\\nKnown words',
        duration: '90', seed: '3',
      });
      const balanced = musicGenerationPlan({
        preset: 'balanced', title: 'Night Signal', caption: 'industrial synth rock',
        lyrics: '[verse]\\nVoltage', duration: '120', seed: '0',
      });
      console.log(JSON.stringify({ invented, known, balanced }));
    """

    plans = run_module(script)
    assert plans["invented"]["endpoint"] == "songplanner"
    assert plans["invented"]["body"]["idea"] == "sunset synthwave idea"
    assert plans["invented"]["body"]["duration"] == 90
    assert plans["invented"]["body"]["seed"] == 7
    assert "lyrics" not in plans["invented"]["body"]
    assert "caption" not in plans["invented"]["body"]
    assert plans["known"]["endpoint"] == "songplanner"
    assert plans["known"]["body"]["idea"] == "faithful synthwave cover"
    assert plans["known"]["body"]["lyrics"] == "[verse]\nKnown words"
    assert plans["known"]["body"]["duration"] == 90
    assert plans["known"]["body"]["seed"] == 3
    assert "caption" not in plans["known"]["body"]
    assert plans["balanced"]["endpoint"] == "music"
    assert plans["balanced"]["body"]["caption"] == "industrial synth rock"
    assert plans["balanced"]["body"]["lyrics"] == "[verse]\nVoltage"


def test_every_markup_preset_resolves_to_its_intended_endpoint():
    presets = markup_preset_values()

    assert set(presets) == set(PRESET_ENDPOINTS), presets
    script = f"""
      import {{ musicGenerationPlan }} from './src/music_video_producer/web/assets/api.js';
      const presets = {json.dumps(presets)};
      const routed = {{}};
      for (const preset of presets) {{
        routed[preset] = musicGenerationPlan({{
          preset, title: 'T', caption: 'an idea', lyrics: '[verse]\\nWords',
          duration: '90', seed: '1',
        }}).endpoint;
      }}
      console.log(JSON.stringify(routed));
    """

    assert run_module(script) == PRESET_ENDPOINTS


def test_known_lyrics_plan_trims_edge_whitespace_but_keeps_interior():
    script = """
      import { musicGenerationPlan } from './src/music_video_producer/web/assets/api.js';
      const interior = '[Verse]\\n\\n  indented\\n\\n[Chorus]\\nNight signal';
      const plan = musicGenerationPlan({
        preset: 'songplanner-known', title: 'T', caption: 'cover',
        lyrics: '\\n\\n  ' + interior + '  \\n\\t', duration: '90', seed: '1',
      });
      const blank = musicGenerationPlan({
        preset: 'songplanner-known', title: 'T', caption: 'cover',
        lyrics: '   \\n\\t ', duration: '90', seed: '1',
      });
      const missing = musicGenerationPlan({
        preset: 'songplanner-known', title: 'T', caption: 'cover', duration: '90', seed: '1',
      });
      console.log(JSON.stringify({ interior, lyrics: plan.body.lyrics, blank: blank.body.lyrics, missing: missing.body.lyrics }));
    """

    result = run_module(script)
    assert result["lyrics"] == result["interior"]
    # An empty sheet stays an empty string so the submit handler can block it with a
    # readable toast instead of dropping the key and silently inventing lyrics.
    assert result["blank"] == ""
    assert result["missing"] == ""


def test_preset_field_state_drives_lyrics_visibility_and_duration_cap():
    script = """
      import { musicPresetFieldState } from './src/music_video_producer/web/assets/api.js';
      const states = {};
      for (const preset of ['balanced', 'songplanner-invented', 'songplanner-known']) {
        states[preset] = musicPresetFieldState(preset);
      }
      console.log(JSON.stringify(states));
    """

    states = run_module(script)
    # Direct Music 3 keeps its own bounds; both SongPlanner presets report the
    # M3SongPlanner node's real 30-300 s range and 32-bit seed, matching
    # SongPlannerRequest so the form can never offer a value the route refuses.
    # Seed bounds are strings because 2**64-1 is not representable as a JS number.
    # Direct Music 3 has no planner, so it has no headroom field either: its bounds are None
    # rather than Music 3's own numbers, so a control that route knows nothing about can never be
    # handed a plausible-looking range.
    assert states["balanced"] == {
        "lyricsVisible": True,
        "lyricsRequired": False,
        "headroomVisible": False,
        "headroomMin": None,
        "headroomMax": None,
        "headroomDefault": None,
        "durationMin": 4,
        "durationMax": 360,
        "seedMin": 0,
        "seedMax": "18446744073709551615",
    }
    assert states["songplanner-invented"] == {
        "lyricsVisible": False,
        "lyricsRequired": False,
        "headroomVisible": True,
        "headroomMin": 1,
        "headroomMax": 12,
        "headroomDefault": 1.5,
        "durationMin": 30,
        "durationMax": 300,
        "seedMin": 0,
        "seedMax": "4294967295",
    }
    assert states["songplanner-known"] == {
        "lyricsVisible": True,
        "lyricsRequired": True,
        "headroomVisible": True,
        "headroomMin": 1,
        "headroomMax": 12,
        "headroomDefault": 1.5,
        "durationMin": 30,
        "durationMax": 300,
        "seedMin": 0,
        "seedMax": "4294967295",
    }


def test_unknown_preset_is_refused_rather_than_treated_as_direct_music_3():
    """Falling through would hand a future SongPlanner variant Music 3's bounds and route."""
    script = """
      import { musicGenerationPlan, musicPresetFieldState } from './src/music_video_producer/web/assets/api.js';
      const attempt = (fn) => { try { fn(); return null; } catch (error) { return error.message; } };
      const results = {};
      for (const preset of ['songplanner-orchestral', '', undefined, 'BALANCED']) {
        results[String(preset)] = {
          plan: attempt(() => musicGenerationPlan({ preset, title: 'T', caption: 'c' })),
          state: attempt(() => musicPresetFieldState(preset)),
        };
      }
      results.balanced = {
        plan: attempt(() => musicGenerationPlan({ preset: 'balanced', title: 'T', caption: 'c' })),
        state: attempt(() => musicPresetFieldState('balanced')),
      };
      console.log(JSON.stringify(results));
    """

    results = run_module(script)
    for preset in ("songplanner-orchestral", "", "undefined", "BALANCED"):
        assert "Unknown song preset" in (results[preset]["plan"] or ""), preset
        assert "Unknown song preset" in (results[preset]["state"] or ""), preset
    assert results["balanced"] == {"plan": None, "state": None}


def model_bound(model, field: str, kind: str):
    """A Pydantic field's `ge`/`le`, or None when the model imposes no such bound."""
    for item in model.model_fields[field].metadata:
        if item.__class__.__name__ == kind:
            return getattr(item, kind.lower())
    return None


def test_form_numeric_bounds_match_the_route_models():
    """A UI bound looser than the route's produces an opaque 422; tighter hides a valid range.

    Reading the bounds off the models means neither side can drift without failing
    here — including the seed ceilings, which differ per route because the planner's
    seed is 32-bit while the encoder and sampler seeds are 64-bit.
    """
    script = """
      import { musicPresetFieldState } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        songplanner: musicPresetFieldState('songplanner-known'),
        music: musicPresetFieldState('balanced'),
      }));
    """
    states = run_module(script)

    for endpoint, model in (("songplanner", SongPlannerRequest), ("music", MusicRequest)):
        assert states[endpoint]["durationMin"] == model_bound(model, "duration", "Ge"), endpoint
        assert states[endpoint]["durationMax"] == model_bound(model, "duration", "Le"), endpoint
        assert states[endpoint]["seedMin"] == model_bound(model, "seed", "Ge"), endpoint
        # int() not float(): 2**64-1 must compare exactly, which is why the JS side
        # carries these as strings.
        assert int(states[endpoint]["seedMax"]) == model_bound(model, "seed", "Le"), endpoint
    # The planner's 32-bit seed is the reason the two presets differ at all.
    assert int(states["songplanner"]["seedMax"]) == 0xFFFFFFFF
    assert int(states["music"]["seedMax"]) == 0xFFFFFFFFFFFFFFFF


def test_the_headroom_control_carries_the_request_models_bounds_and_its_default():
    """The form's multiplier bounds are `SongPlannerRequest.duration_headroom`'s, read off it.

    The same drift guard the duration and seed bounds get, for the same reason and one more: the
    *default* is on the form too, because the form now sends the field on every submission rather
    than letting it be defaulted invisibly. A hand-typed 1.5 in the markup or in `api.js` that the
    model later moved away from would put a number on screen, send it, and be right about neither.

    1.0 has to survive all of this. It reproduces the pre-headroom payload byte for byte and is one
    of the two candidate answers to a question no live render has settled, so a floor that rounded
    it away would remove the comparison the Director needs to make.
    """
    script = """
      import { musicPresetFieldState } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        invented: musicPresetFieldState('songplanner-invented'),
        known: musicPresetFieldState('songplanner-known'),
        music: musicPresetFieldState('balanced'),
      }));
    """
    states = run_module(script)
    field = SongPlannerRequest.model_fields["duration_headroom"]

    for preset in ("invented", "known"):
        assert states[preset]["headroomVisible"] is True, preset
        assert states[preset]["headroomMin"] == model_bound(
            SongPlannerRequest, "duration_headroom", "Ge"
        ), preset
        assert states[preset]["headroomMax"] == model_bound(
            SongPlannerRequest, "duration_headroom", "Le"
        ), preset
        assert states[preset]["headroomDefault"] == field.default, preset
        # Stated outright as well as derived: the floor is the setting's whole comparison.
        assert states[preset]["headroomMin"] == 1.0, preset

    # Direct Music 3 has no planner and `MusicRequest` has no such field, so the control is absent
    # rather than bounded — offering it there would send a key the route would reject.
    assert states["music"]["headroomVisible"] is False
    assert "duration_headroom" not in MusicRequest.model_fields
    for bound in ("headroomMin", "headroomMax", "headroomDefault"):
        assert states["music"][bound] is None, bound


def test_every_preset_that_has_a_headroom_control_sends_its_value():
    """Every SongPlanner variant, not just the one the workspace test happens to drive.

    Found by mutation: dropping `duration_headroom` from the invented-lyrics body alone left the
    whole suite green, because the booted-workspace test drives the known-lyrics preset. Half a fix
    is the same regression wearing the other preset's name — the form would show a multiplier, the
    Director would set it, and the route would default it to 1.5 behind their back on exactly the
    long invented-lyrics song this setting exists to be judged on. So the presets are enumerated
    from the markup and each one's plan is checked against whether it has the control at all.
    """
    presets = markup_preset_values()
    script = f"""
      import {{ musicGenerationPlan, musicPresetFieldState }}
        from './src/music_video_producer/web/assets/api.js';
      const planned = {{}};
      for (const preset of {json.dumps(presets)}) {{
        const plan = musicGenerationPlan({{
          preset, title: 'T', caption: 'an idea', lyrics: '[verse]\\nWords',
          duration: '90', duration_headroom: '2.5', seed: '1',
        }});
        planned[preset] = {{
          hasControl: musicPresetFieldState(preset).headroomVisible,
          sent: Object.hasOwn(plan.body, 'duration_headroom') ? plan.body.duration_headroom : null,
        }};
      }}
      console.log(JSON.stringify(planned));
    """
    planned = run_module(script)

    assert set(planned) == set(PRESET_ENDPOINTS), planned
    for preset, outcome in planned.items():
        # The control and the key on the wire are the same fact seen twice: a preset that shows the
        # box must send it, and one that does not must not send a field its route has never heard
        # of. Either mismatch is a form saying one thing and a request doing another.
        assert outcome["hasControl"] is (PRESET_ENDPOINTS[preset] == "songplanner"), preset
        assert outcome["sent"] == (2.5 if outcome["hasControl"] else None), (preset, outcome)


def test_the_form_shows_the_product_rather_than_bounding_either_duration_field():
    """The chosen answer to the two fields' interaction, executed.

    `duration` x `duration_headroom` has to stay inside the encoder's 360 s schema ceiling, so at
    the default 1.5 a 300 s song is refused — the regression this change exists to close, where the
    form's own `max` of 300 promised a duration the route would 422.

    Bounding either field against the other was rejected: whichever follows becomes a trap, because
    raising one slides the other's `max` under a number already in its box, and the only ways out of
    that are silently rewriting what the Director typed — the truncation this whole feature guards
    against — or leaving a box holding a value its own `max` forbids. So neither `max` moves, and
    the *product*, which is what the schema actually bounds, is shown instead and refused locally
    when it leaves the range. This test pins both halves: the bounds that must not move, and the
    product that must be reported.
    """
    script = """
      import { musicFormFieldUpdate, songEncoderCeiling, MUSIC3_MAX_DURATION_SECONDS, CEILING_UNSET }
        from './src/music_video_producer/web/assets/api.js';
      const at = (duration, headroom) => songEncoderCeiling(duration, headroom);
      console.log(JSON.stringify({
        ceiling: MUSIC3_MAX_DURATION_SECONDS,
        unset: CEILING_UNSET,
        // The bounds each field reports while the other one moves across its whole range.
        bounds: [1, 1.5, 12].map((headroom) => {
          const update = musicFormFieldUpdate('songplanner-known', { duration: '300', headroom, duration_headroom: String(headroom) });
          return {
            headroom,
            durationMax: update.numeric.duration.max,
            durationValue: update.numeric.duration.value,
            headroomMax: update.numeric.duration_headroom.max,
            headroomValue: update.numeric.duration_headroom.value,
          };
        }),
        inRange: at('120', '1.5'),
        exactlyAtTheCeiling: at('240', '1.5'),
        overByTheDefault: at('300', '1.5'),
        noHeadroomAtAll: at('300', '1'),
        widest: at('30', '12'),
        overAtTheWidest: at('31', '12'),
        emptyDuration: at('', '1.5'),
        emptyHeadroom: at('120', ''),
        notANumber: at('abc', '1.5'),
      }));
    """
    result = run_module(script)

    # The encoder's ceiling is the builder's own constant, not a number retyped in the browser.
    assert result["ceiling"] == MUSIC3_MAX_DURATION_SECONDS

    # Neither field's bound follows the other, and neither value is rewritten, at any headroom.
    for row in result["bounds"]:
        assert row["durationMax"] == model_bound(SongPlannerRequest, "duration", "Le"), row
        assert row["durationValue"] == "300", row
        assert row["headroomMax"] == model_bound(SongPlannerRequest, "duration_headroom", "Le"), row
        assert float(row["headroomValue"]) == row["headroom"], row

    # The product, in the words the Director reads, naming both node inputs' roles.
    assert result["inRange"]["ceiling"] == 180
    assert result["inRange"]["exceeds"] is False
    assert result["inRange"]["refusal"] is None
    assert "120 s" in result["inRange"]["text"] and "180 s" in result["inRange"]["text"]
    assert "360 s" in result["inRange"]["text"]

    # 240 x 1.5 is exactly 360: the largest submittable request at the shipped default, and the
    # boundary the whole regression is about. Inside, not over.
    assert result["exactlyAtTheCeiling"]["ceiling"] == MUSIC3_MAX_DURATION_SECONDS
    assert result["exactlyAtTheCeiling"]["exceeds"] is False

    # And one second of song past it is refused, in a sentence that names both ways out and
    # neither of the Director's numbers as the one that has to give.
    refused = result["overByTheDefault"]
    assert refused["exceeds"] is True
    assert refused["refusal"] == refused["text"], "the readout and the refusal are two wordings"
    assert "450 s" in refused["text"], refused["text"]
    assert "360 s" in refused["text"], refused["text"]
    # 360 / 300 and 360 / 1.5 — both alternatives computed, so neither is a number to type back in
    # and be refused again.
    assert "1.2" in refused["text"], refused["text"]
    assert "240 s" in refused["text"], refused["text"]

    # A headroom of 1.0 is the pre-headroom behaviour, and it is exactly what makes the form's own
    # 300 s maximum submittable again.
    assert result["noHeadroomAtAll"]["ceiling"] == 300
    assert result["noHeadroomAtAll"]["exceeds"] is False
    assert result["noHeadroomAtAll"]["refusal"] is None
    # 30 x 12 is the widest legal product the two fields can make; a second more is not.
    assert result["widest"]["exceeds"] is False
    assert result["overAtTheWidest"]["exceeds"] is True

    # A half-filled pair states nothing rather than inventing a product from one box.
    for absent in ("emptyDuration", "emptyHeadroom", "notANumber"):
        assert result[absent]["ceiling"] is None, absent
        assert result[absent]["exceeds"] is False, absent
        assert result[absent]["text"] == result["unset"], absent


def test_form_field_update_applies_bounds_and_clamps_per_preset():
    """Executable coverage for the logic syncMusicVariant applies to the DOM.

    The old test only grepped the handler for identifier names, so a mutation that
    kept the names but inverted a clamp or dropped an assignment survived the suite.
    """
    script = """
      import { musicFormFieldUpdate } from './src/music_video_producer/web/assets/api.js';
      const cases = {
        aboveCeiling: musicFormFieldUpdate('songplanner-known', { duration: '360', seed: '5' }),
        belowFloor: musicFormFieldUpdate('songplanner-invented', { duration: '4', seed: '5' }),
        inRange: musicFormFieldUpdate('songplanner-known', { duration: '90.5', seed: '5' }),
        seedAboveCeiling: musicFormFieldUpdate('songplanner-known', { duration: '90', seed: '4294967296' }),
        backToBalanced: musicFormFieldUpdate('balanced', { duration: '30', seed: '4294967296' }),
        cleared: musicFormFieldUpdate('balanced', { duration: '', seed: '' }),
        notANumber: musicFormFieldUpdate('songplanner-known', { duration: 'abc', seed: 'abc' }),
        absent: musicFormFieldUpdate('songplanner-known'),
      };
      console.log(JSON.stringify(cases));
    """

    cases = run_module(script)
    assert cases["aboveCeiling"]["numeric"]["duration"] == {"min": 30, "max": 300, "value": 300}
    assert cases["belowFloor"]["numeric"]["duration"] == {"min": 30, "max": 300, "value": 30}
    # An in-range fractional value must survive untouched, exactly as typed.
    assert cases["inRange"]["numeric"]["duration"]["value"] == "90.5"
    assert cases["seedAboveCeiling"]["numeric"]["seed"]["value"] == "4294967295"
    # Switching back to a preset with a wider range must not clamp anything down.
    assert cases["backToBalanced"]["numeric"]["duration"]["value"] == "30"
    assert cases["backToBalanced"]["numeric"]["seed"]["value"] == "4294967296"
    # `Number("")` is 0: a cleared box must stay cleared, not acquire the minimum.
    assert cases["cleared"]["numeric"]["duration"]["value"] == ""
    assert cases["cleared"]["numeric"]["seed"]["value"] == ""
    # NaN compares false against both bounds, so it must be left alone rather than
    # silently passed through as a clamped number.
    assert cases["notANumber"]["numeric"]["duration"]["value"] == "abc"
    assert cases["absent"]["numeric"]["duration"]["value"] == ""
    assert cases["absent"]["numeric"]["seed"]["max"] == "4294967295"
    assert cases["belowFloor"]["lyricsVisible"] is False
    assert cases["aboveCeiling"]["lyricsRequired"] is True
    assert cases["backToBalanced"]["lyricsRequired"] is False


def test_form_field_update_seeds_the_headroom_and_leaves_it_where_the_director_put_it():
    """The multiplier's own lifecycle, separate from the product it takes part in.

    An empty box seeds the model's default instead of staying cleared, which is where this field
    parts company with `duration`: a cleared duration is a Director mid-edit, but an absent
    multiplier is not a request at all, and the whole point of the control is that the number in
    force is on screen rather than applied by a default nobody saw. Everything else about it is the
    duration's rules — clamped to its own bounds, fractional values kept exactly as typed.
    """
    script = """
      import { musicFormFieldUpdate } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        empty: musicFormFieldUpdate('songplanner-known', { duration: '120', duration_headroom: '' }),
        absent: musicFormFieldUpdate('songplanner-invented', { duration: '120' }),
        one: musicFormFieldUpdate('songplanner-known', { duration: '120', duration_headroom: '1' }),
        fractional: musicFormFieldUpdate('songplanner-known', { duration: '120', duration_headroom: '2.25' }),
        belowFloor: musicFormFieldUpdate('songplanner-known', { duration: '120', duration_headroom: '0.5' }),
        aboveCeiling: musicFormFieldUpdate('songplanner-known', { duration: '120', duration_headroom: '99' }),
        notANumber: musicFormFieldUpdate('songplanner-known', { duration: '120', duration_headroom: 'abc' }),
        balanced: musicFormFieldUpdate('balanced', { duration: '120', duration_headroom: '2.25' }),
      }));
    """
    cases = run_module(script)
    default = SongPlannerRequest.model_fields["duration_headroom"].default

    # Nothing in the box, and something on screen: the multiplier in force is never invisible.
    assert cases["empty"]["numeric"]["duration_headroom"]["value"] == default
    assert cases["absent"]["numeric"]["duration_headroom"]["value"] == default
    # 1.0 is a value the Director can hold, not a floor that rounds up to the default.
    assert cases["one"]["numeric"]["duration_headroom"]["value"] == "1"
    assert cases["fractional"]["numeric"]["duration_headroom"]["value"] == "2.25"
    # Clamped to this field's own model bounds, and to nothing else.
    assert cases["belowFloor"]["numeric"]["duration_headroom"]["value"] == 1
    assert cases["aboveCeiling"]["numeric"]["duration_headroom"]["value"] == 12
    assert cases["notANumber"]["numeric"]["duration_headroom"]["value"] == "abc"
    # Direct Music 3 has no such control, so the update carries no entry to write onto one — which
    # is also what leaves a chosen multiplier untouched by a trip through the Balanced preset.
    assert "duration_headroom" not in cases["balanced"]["numeric"]
    assert cases["balanced"]["headroomVisible"] is False


def test_the_song_form_sends_the_headroom_it_shows_and_refuses_what_the_route_would():
    """The regression, closed, in the workspace's own code rather than in a function beside it.

    Three things a pure-function test cannot reach, so `app.js` is booted and driven: that the
    handler puts the ceiling on screen at all, that the submission carries `duration_headroom`
    instead of letting the route default it invisibly, and that a product past the encoder's
    schema ceiling is stopped before it costs the Director a confirmation — the harness's
    `window.confirm` throws until a test answers it, so a question asked here would fail loudly.
    """
    driven = run_workspace("""
      // A FormData the stub DOM can produce: the real one takes an HTMLFormElement, which does not
      // exist here. It reproduces the two behaviours the submit handler leans on -- values come
      // from the controls themselves, and a `disabled` control is not submitted at all -- over the
      // form's own field names, which is what makes the direct Music 3 case below mean anything.
      const NAMES = ['title', 'caption', 'lyrics', 'duration', 'duration_headroom', 'seed', 'preset'];
      globalThis.FormData = class {
        constructor(form) {
          this.pairs = NAMES
            .filter((name) => !form.elements[name].disabled)
            .map((name) => [name, form.elements[name].value]);
        }
        [Symbol.iterator]() { return this.pairs[Symbol.iterator](); }
      };
      const toasts = [];
      at('#toast-region').append = (item) => toasts.push(item.textContent);
      const form = at('#music-form');
      form.elements.title.value = 'Night Signal';
      form.elements.caption.value = 'sunset synthwave';
      form.elements.lyrics.value = '[Verse]\\nKnown words';
      // The stub creates every control blank, so the duration the markup ships with is put back
      // here; the headroom's is deliberately not, because seeding that one is the code's job.
      form.elements.duration.value = '120';
      form.elements.seed.value = '0';
      state.project = { id: 'p1', shots: [], jobs: [], song: null };
      const readout = () => ({
        text: at('#music-ceiling').textContent,
        over: at('#music-ceiling').classList.contains('over'),
        shown: at('#music-headroom-field').style.display,
        disabled: form.elements.duration_headroom.disabled,
        value: form.elements.duration_headroom.value,
      });
      const type = (name, value) => { form.elements[name].value = value; fire('#music-form[' + name + ']:input'); };
      const submit = () => fire('#music-form:submit', { preventDefault() {}, currentTarget: form });

      // The workspace fires its own startup requests on import and they all reject; let them
      // settle and clear the record, so what is counted below is this form's doing alone.
      await flush();
      requests.length = 0;
      toasts.length = 0;

      // Boots on Balanced: no planner, so no control and nothing on the wire to carry.
      const balanced = readout();

      form.elements.preset.value = 'songplanner-known';
      fire('#music-form[preset]:change');
      const seeded = readout();

      // The duration the form's own `max` has always offered, at the shipped default.
      type('duration', '300');
      const overCeiling = readout();

      // Refused here, with no reply canned and `window.confirm` still throwing: a request or a
      // question at this point is a failure, not a pass.
      await submit();
      const refusal = { toasts: [...toasts], requests: requests.length };

      // The Director takes one of the two ways out the sentence named.
      toasts.length = 0;
      type('duration_headroom', '1');
      const relieved = readout();
      answer(true);
      await submit();
      await flush();
      const sentPlanner = requests.map((entry) => ({ path: entry.path, body: JSON.parse(entry.body) }));

      // And back to the route that has no such field.
      answer(true);
      form.elements.preset.value = 'balanced';
      fire('#music-form[preset]:change');
      const backToBalanced = readout();
      await submit();
      await flush();
      const sentMusic = requests.map((entry) => ({ path: entry.path, body: JSON.parse(entry.body) }));

      console.log(JSON.stringify({ balanced, seeded, overCeiling, refusal, relieved, sentPlanner, backToBalanced, sentMusic }));
    """)
    default = SongPlannerRequest.model_fields["duration_headroom"].default

    # Direct Music 3: the control, its readout and the note naming the two node inputs are all gone
    # together, and the box is disabled so a `required` field nobody can see cannot block a submit.
    assert driven["balanced"]["shown"] == "none"
    assert driven["balanced"]["disabled"] is True

    # SongPlanner: the box appears carrying the model's default, and the ceiling it produces is on
    # screen without the Director touching anything.
    assert driven["seeded"]["shown"] == ""
    assert driven["seeded"]["disabled"] is False
    assert driven["seeded"]["value"] == default
    assert "180 s" in driven["seeded"]["text"], driven["seeded"]["text"]
    assert driven["seeded"]["over"] is False

    # 300 s at 1.5 is the regression itself: a duration the form's `max` offers and the route
    # refuses. The form now says so before the submit rather than after it.
    assert driven["overCeiling"]["over"] is True
    assert "450 s" in driven["overCeiling"]["text"], driven["overCeiling"]["text"]

    # And stops it, in the same words, having spent nothing.
    assert driven["refusal"]["requests"] == 0, "the submit reached the network anyway"
    assert driven["refusal"]["toasts"] == [driven["overCeiling"]["text"]], driven["refusal"]

    # A headroom of 1.0 is reachable, and it is what makes the form's own 300 s maximum submittable
    # again — the pre-headroom payload, byte for byte, from the UI.
    assert driven["relieved"]["over"] is False
    assert driven["sentPlanner"] == [
        {
            "path": "/api/projects/p1/generate/songplanner",
            "body": {
                "title": "Night Signal",
                "idea": "sunset synthwave",
                "lyrics": "[Verse]\nKnown words",
                "duration": 300,
                "duration_headroom": 1,
                "seed": 0,
                "confirm_song_replacement": False,
            },
        }
    ]

    # The direct route has no planner and no such field, so it must not acquire the key on the way
    # past — and the multiplier the Director chose survives the round trip through Balanced.
    assert driven["backToBalanced"]["value"] == "1"
    assert len(driven["sentMusic"]) == 1
    assert driven["sentMusic"][0]["path"] == "/api/projects/p1/generate/music"
    assert "duration_headroom" not in driven["sentMusic"][0]["body"]


def test_the_headroom_field_names_the_two_inputs_it_separates_and_does_not_vouch_for_1_5():
    """The copy is the larger half of this change, so its removal has to fail something.

    The two inputs take the same kind of number and mean different things; a form that shows a
    multiplier without saying what it multiplies into is the conflation this feature exists to
    undo. And the 1.5 it ships at is a documented claim contradicted by the same creator's own
    exported graphs, with no live render long enough to tell them apart — so the copy reports the
    disagreement rather than presenting the number as verified.
    """
    markup = INDEX_HTML.read_text(encoding="utf-8")
    block = re.search(r'<div id="music-headroom-field">.*?\n                </div>', markup, re.DOTALL)
    assert block, "the Song workspace form no longer has a headroom block"
    # The comments are the implementer's; the Director reads only what is outside a tag.
    help_text = re.search(r'<p class="field-help">(.*?)</p>', block.group(0), re.DOTALL)
    assert help_text, "the headroom block no longer explains what it multiplies"
    prose = re.sub(r"<[^>]+>", "", help_text.group(1))

    # Both node inputs, named, where the Director reads them rather than in a tooltip.
    assert "M3SongPlanner" in prose, prose
    assert "MiniMaxMusic3TextEncode.max_duration" in prose, prose
    # What each one governs: a length to write, against a ceiling that may be finished before.
    assert "how long a song" in prose, prose
    assert "ceiling" in prose, prose
    # 1.0's meaning, since it is one of the two candidate answers and the reproducible one.
    assert "1.0" in prose, prose
    # And the contradiction, in the same breath as the number.
    assert "1.5" in prose, prose
    assert "set both inputs equal" in prose, prose

    field = re.search(r'<input name="duration_headroom"[^>]*>', block.group(0))
    assert field, "the headroom input is gone"
    # The route takes a float; the browser's default step=1 would refuse the 1.5 it ships at.
    assert 'step="any"' in field.group(0), field.group(0)
    # No bound and no default in the markup: every one of them comes from musicFormFieldUpdate,
    # which is held equal to the request model. A number here is exactly the drift that guards
    # against, so its absence is asserted rather than assumed.
    for retyped in ("min=", "max=", "value="):
        assert retyped not in field.group(0), field.group(0)


def test_duration_input_accepts_fractional_values():
    """The default step=1 would make the browser refuse durations the route accepts."""
    markup = INDEX_HTML.read_text(encoding="utf-8")
    duration = re.search(r'<input name="duration"[^>]*>', markup)

    assert duration, "the Song workspace form no longer has a duration input"
    assert 'step="any"' in duration.group(0), duration.group(0)


def test_sync_music_variant_is_a_thin_applier_over_the_shared_update():
    """Source-level companion to test_form_field_update_applies_bounds_and_clamps_per_preset.

    The behaviour is asserted there, executably; all this pins is that the handler
    still delegates instead of re-deriving bounds or preset logic in the DOM layer,
    where nothing can test it.
    """
    handler = APP_JS.read_text(encoding="utf-8").split("const syncMusicVariant", 1)[1].split("};", 1)[0]

    assert "musicFormFieldUpdate(musicForm.elements.preset.value" in handler
    assert "update.lyricsVisible" in handler
    assert "update.lyricsRequired" in handler
    assert "update.numeric" in handler
    # Bounds, clamping and preset logic all belong to api.js, not to this handler.
    for leaked in ("songplanner-known", "songplanner-invented", "durationMin", "durationMax",
                   "seedMax", "Number(", "clamp"):
        assert leaked not in handler, leaked


def test_song_import_duration_never_inherits_a_previous_songs_length():
    """FR-12's ffprobe fallback only runs when the browser sends 0.

    The frontend defeated it: a failed decode left `state.audioBuffer` holding the
    previously loaded song, so importing an undecodable file into a project that
    already had a decodable song sent *that* song's duration. The server saw a
    non-zero value, skipped ffprobe, and persisted a wrong timing spine. Grepping the
    handler for identifier names could never catch that, so the decision is a pure
    function and this executes it.
    """
    script = """
      import { songImportDuration } from './src/music_video_producer/web/assets/api.js';
      const previous = { duration: 187.5 };
      console.log(JSON.stringify({
        decoded: songImportDuration({ decoded: { duration: 42.75 } }),
        failed: songImportDuration({ decoded: null }),
        failedWithPrevious: songImportDuration({ decoded: null, previous }),
        failedWithPreviousUndefined: songImportDuration({ previous }),
        noPending: songImportDuration(),
        nullPending: songImportDuration(null),
        zeroLength: songImportDuration({ decoded: { duration: 0 } }),
        notANumber: songImportDuration({ decoded: { duration: NaN } }),
        infinite: songImportDuration({ decoded: { duration: Infinity } }),
        negative: songImportDuration({ decoded: { duration: -3 } }),
      }));
    """

    durations = run_module(script)
    # A successful decode is the only thing trusted, and it is sent exactly.
    assert durations["decoded"] == 42.75
    # Every "unknown length" shape must be exactly 0 — the one value that makes
    # app.py's `resolved_duration` reach for ffprobe.
    for unknown in ("failed", "failedWithPrevious", "failedWithPreviousUndefined",
                    "noPending", "nullPending", "zeroLength", "notANumber",
                    "infinite", "negative"):
        assert durations[unknown] == 0, unknown


def test_song_import_handlers_drop_a_failed_decode_and_delegate_the_duration():
    """Source-level companion to the pure assertion above.

    The pure function cannot be handed a stale buffer if the handler never keeps
    one, and nothing else in the suite reaches these two DOM handlers.
    """
    source = APP_JS.read_text(encoding="utf-8")
    change = source.split('$("#song-file").addEventListener("change"', 1)[1].split("  });", 1)[0]
    importer = source.split('$("#import-song").addEventListener("click"', 1)[1].split("  });", 1)[0]

    # The decode-failure path must drop the buffer; retaining it was the first defect.
    assert "catch {" in change
    assert "state.audioBuffer = null;" in change.split("catch {", 1)[1]

    # Dropping it is not enough on its own: loadPersistedWaveform is a second, un-awaited
    # writer of state.audioBuffer for the *stored* song, and it could land after this
    # handler and hand the import that song's length. So the candidate's measurement is
    # recorded against the File it came from, and both clearing paths bump the revision
    # counter that cancels an in-flight persisted decode.
    assert "state.pendingImport = { file, decoded: null }" in change
    assert "waveformLoadRevision += 1;" in change
    assert "state.pendingImport = { file, decoded }" in change

    # Rendering must sit outside the decode's catch: a throw from renderSong is not a
    # decode failure, and treating it as one discards a perfectly good buffer.
    assert "renderSong();" not in change.split("try {", 1)[1].split("catch", 1)[0]

    # The import reads only that file's own record -- never the shared buffer, which is
    # what silently carried the previous song's length.
    assert "state.pendingImport?.file === file" in importer
    assert "songImportDuration(" in importer
    assert "state.audioBuffer" not in importer


def import_block() -> str:
    """The markup of the Song workspace's import block, from its heading to its own end."""
    block = re.search(
        r'<article class="tool-block">\s*<div class="block-heading"><h2>Import master</h2>.*?</article>',
        INDEX_HTML.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert block, "the Song workspace no longer has an import block"
    return block.group(0)


def music_form_markup() -> str:
    """The markup of the generation form, whose lyrics textarea is generation input."""
    form = re.search(
        r'<form id="music-form">.*?</form>', INDEX_HTML.read_text(encoding="utf-8"), re.DOTALL
    )
    assert form, "the Song workspace no longer has a generation form"
    return form.group(0)


def app_js_handler(anchor: str) -> str:
    """One DOM event handler's source, by the selector it is bound to."""
    source = APP_JS.read_text(encoding="utf-8")
    assert anchor in source, anchor
    return source.split(anchor, 1)[1].split("  });", 1)[0]


def test_song_context_fields_map_the_style_box_to_caption_and_trim_only_edges():
    """The one mapping both song-context paths use, executed rather than grepped.

    Which box lands on which field is invisible on screen and invisible in a diff: a lyric sheet
    stored as `caption` reaches the Director as a description of how the song sounds, and a style
    line stored as `lyrics` is read as the words being sung. Both are plausible strings in the
    wrong slot, so nothing downstream would fail — which is why the arguments' order and their
    destinations are asserted directly, with two values that cannot be confused for one another.
    """
    script = """
      import { songContextFields } from './src/music_video_producer/web/assets/api.js';
      const interior = '[Verse 1]\\n\\n    indented line\\n\\n[Chorus]\\nHold the line';
      console.log(JSON.stringify({
        interior,
        crossed: songContextFields('THE-LYRIC-SHEET', 'THE-STYLE-LINE'),
        trimmed: songContextFields('\\n\\n  ' + interior + '  \\n\\t', '  synthwave, close vocal  '),
        blank: songContextFields('   \\n\\t ', '\\n\\n'),
        absent: songContextFields(),
        nulls: songContextFields(null, undefined),
      }));
    """

    result = run_module(script)
    # The lyric box is the lyric sheet and the style box is the caption -- never the other way.
    assert result["crossed"] == {"lyrics": "THE-LYRIC-SHEET", "caption": "THE-STYLE-LINE"}
    # Edges only: every interior blank line, indent and section tag survives byte for byte, which
    # is the server's contract in `_song_context` and the known-lyrics path's before it.
    assert result["trimmed"]["lyrics"] == result["interior"]
    assert result["trimmed"]["caption"] == "synthwave, close vocal"
    # Whitespace-only is absent, and so is a control that was never typed into.
    for empty in ("blank", "absent", "nulls"):
        assert result[empty] == {"lyrics": "", "caption": ""}, empty


def test_the_song_context_editor_follows_the_song_rather_than_the_project():
    script = """
      import { songContextEditable } from './src/music_video_producer/web/assets/api.js';
      const song = { title: 'Spine', source: 'imported', path: 'media/songs/a.wav', duration: 180 };
      console.log(JSON.stringify({
        withSong: songContextEditable({ song }),
        withoutSong: songContextEditable({ song: null }),
        noProject: songContextEditable(null),
        absent: songContextEditable(),
      }));
    """

    editable = run_module(script)

    assert editable["withSong"] is True
    for disabled in ("withoutSong", "noProject", "absent"):
        assert editable[disabled] is False, disabled


def test_the_import_context_controls_are_not_the_generation_forms_lyrics_field():
    """The two lyrics controls are different controls, and neither handler reaches for the other.

    Sending generation input to an import -- or a finished master's lyric sheet into a song being
    written -- is worse than the gap this closes: both are plausible text arriving at a route that
    accepts it, so nothing fails and the Director is never told. The separation is structural
    (distinct ids, on controls outside every form) and it is asserted on both halves: the markup,
    so one control cannot become the other, and the handlers, so neither reads the other's.
    """
    markup = INDEX_HTML.read_text(encoding="utf-8")
    imports = import_block()
    generation = music_form_markup()

    # The import's fields live in the import block, and nowhere else in the document.
    for control in ('id="import-lyrics"', 'id="import-style"'):
        assert control in imports, control
        assert markup.count(control) == 1, control
        assert control not in generation, control
    # The generation form keeps its own, reached by name from the form's own FormData.
    assert 'name="lyrics"' in generation
    assert 'name="lyrics"' not in imports
    # And the loaded-song editors are a third, separate pair -- in the stage, not in either.
    for editor in ('id="song-lyrics"', 'id="song-style"', 'id="save-song-context"'):
        assert markup.count(editor) == 1, editor
        assert editor not in imports, editor
        assert editor not in generation, editor

    importer = app_js_handler('$("#import-song").addEventListener("click"')
    generator = app_js_handler('musicForm.addEventListener("submit"')

    # The import reads the import block's controls and nothing else: not the generation form's
    # element collection, and not the loaded song's editors.
    assert '$("#import-lyrics").value' in importer
    assert '$("#import-style").value' in importer
    for foreign in ("musicForm", "elements.lyrics", "#song-lyrics", "#song-style", "data.lyrics"):
        assert foreign not in importer, foreign
    # And the generation form never reaches for the import's fields or the import route.
    for foreign in ("import-lyrics", "import-style", "songContextFields", "uploadSong"):
        assert foreign not in generator, foreign


def test_the_import_sends_the_song_context_the_director_typed():
    """What the Director typed reaches the multipart body, under the server's own field names.

    The route reads `lyrics` and `caption` off the form; a body that spelled either differently
    would be accepted as an import carrying neither, which is silence rather than a failure.
    """
    importer = app_js_handler('$("#import-song").addEventListener("click"')

    assert 'songContextFields($("#import-lyrics").value, $("#import-style").value)' in importer
    assert 'form.append("lyrics", context.lyrics)' in importer
    assert 'form.append("caption", context.caption)' in importer
    # Both appends are before the upload, or they are not in the request at all.
    assert importer.index('form.append("caption"') < importer.index("api.uploadSong(")


def test_the_song_context_editor_saves_only_the_two_context_fields():
    """The edit is context and nothing else, and it does not overwrite the Director mid-paste.

    `renderSong` runs on far more than a project load -- the audio element's `loadedmetadata` fires
    one -- so re-seeding the editors unconditionally would delete a lyric sheet while it was being
    pasted. The dirty flag is the guard, and only a project load or a landed save clears it.
    """
    # Comments dropped: the assertions below are about what the handler *does*, and the sentence
    # explaining why it does it legitimately contains words like "path".
    save = without_comments(app_js_block("async function saveSongContext()"))
    render = without_comments(app_js_block("function renderSongContext()"))

    assert 'songContextFields($("#song-lyrics").value, $("#song-style").value)' in save
    assert "api.saveSongContext(state.project.id, context)" in save
    # Nothing about the audio, its length or its provenance is sent, and the import's fields are
    # not read here either.
    for foreign in ("path", "duration", "source", "prompt_id", "import-lyrics", "import-style"):
        assert foreign not in save, foreign
    # The flag is cleared only after the server has answered; until then the screen is the only copy.
    assert save.index("api.saveSongContext(") < save.index("state.songContextDirty = false")

    assert "if (!state.songContextDirty) {" in render
    assert 'lyrics.value = song?.lyrics || "";' in render
    assert 'style.value = song?.caption || "";' in render
    # A project load re-seeds them, or a switch leaves the previous project's sheet on screen.
    load = app_js_block("async function loadProject(id)")
    assert "state.songContextDirty = false;" in load
    assert load.index("state.songContextDirty = false;") < load.index("renderAll();")


def test_rendering_the_song_seeds_and_enables_the_context_editors():
    """The one call that makes the whole edit-after-import path exist, executed rather than read.

    `renderSongContext` is reached from a single line inside `renderSong`, and it is the only thing
    that seeds the two boxes from the stored Song and the only thing that clears the `disabled`
    state `index.html` ships on both boxes and on the save button. Delete that line and the editors
    are never filled and never enabled -- in every project, for the entire feature -- while a test
    that reads the function's source out of the file still passes, because the function is still
    there. So the render is run here, against a stub DOM, and the boxes are read afterwards.
    """
    rendered = run_workspace("""
      const sheet = '[Verse 1]\\n\\n    counting sodium lights\\n\\n[Chorus]\\nHold the line';
      const song = { title: 'Spine', source: 'imported', path: 'media/songs/000-master.wav', duration: 180, lyrics: sheet, caption: 'synthwave, close vocal' };
      const read = () => ({
        lyrics: at('#song-lyrics').value,
        style: at('#song-style').value,
        lyricsDisabled: at('#song-lyrics').disabled,
        styleDisabled: at('#song-style').disabled,
        saveDisabled: at('#save-song-context').disabled,
        count: at('#song-lyrics-count').textContent,
        over: at('#song-lyrics-count').classList.contains('over'),
      });
      state.project = { id: 'p1', shots: [], jobs: [], song };
      state.songContextDirty = false;
      app.renderSong();
      const seeded = read();
      // An incidental render -- the audio element's `loadedmetadata` fires one -- must not delete
      // what the Director is part-way through typing.
      state.songContextDirty = true;
      at('#song-lyrics').value = 'MID-PASTE';
      app.renderSong();
      const midPaste = read();
      // With no song there is nothing to describe and the route would 404, so the block is shut.
      state.songContextDirty = false;
      state.project = { id: 'p2', shots: [], jobs: [], song: null };
      app.renderSong();
      const songless = read();
      // An oversized stored sheet reports itself as unsaveable rather than being silently cut.
      state.project = { id: 'p3', shots: [], jobs: [], song: { ...song, lyrics: 'x'.repeat(8001) } };
      app.renderSong();
      const oversized = read();
      console.log(JSON.stringify({ sheet, seeded, midPaste, songless, oversized }));
    """)

    # Seeded from the stored Song, interior structure and all, and the style box from `caption`.
    assert rendered["seeded"]["lyrics"] == rendered["sheet"]
    assert rendered["seeded"]["style"] == "synthwave, close vocal"
    # And enabled: the markup ships all three disabled, so this is the only thing that opens them.
    assert rendered["seeded"]["lyricsDisabled"] is False
    assert rendered["seeded"]["styleDisabled"] is False
    assert rendered["seeded"]["saveDisabled"] is False
    assert rendered["seeded"]["count"] == "61 / 8,000"
    assert rendered["seeded"]["over"] is False

    # Unsaved typing survives a render it did not ask for.
    assert rendered["midPaste"]["lyrics"] == "MID-PASTE"
    assert rendered["midPaste"]["style"] == "synthwave, close vocal"

    # A song-less project: cleared and shut, so no blank context can be PUT at a 404.
    assert rendered["songless"]["lyrics"] == ""
    assert rendered["songless"]["style"] == ""
    assert rendered["songless"]["lyricsDisabled"] is True
    assert rendered["songless"]["styleDisabled"] is True
    assert rendered["songless"]["saveDisabled"] is True

    assert rendered["oversized"]["over"] is True
    assert "too long to save" in rendered["oversized"]["count"]


def test_unsaved_song_context_asks_before_a_switch_and_before_a_tab_close():
    """A lyric sheet is unsaved work, and both navigation guards now say so.

    `songContextDirty` is deliberately not part of `state.dirty` -- it also decides whether an
    incidental render may re-seed the boxes, which `state.dirty` must never do -- and `state.dirty`
    was what gated the project-switch question and the `beforeunload` guard. So 8000 characters of
    pasted lyrics were discarded without a word on a switch or a tab close, while three characters
    typed into a document textarea produced a question. Both guards are fired here rather than read.
    """
    guards = run_workspace("""
      const song = { title: 'Spine', source: 'imported', path: 'media/songs/000-master.wav', duration: 180, lyrics: 'stored words', caption: 'stored style' };
      const attempt = async (flags) => {
        state.project = { id: 'p1', shots: [], jobs: [], song };
        Object.assign(state, { dirty: false, documentsDirty: false, shotsDirty: false, songContextDirty: false }, flags);
        answer(false);
        const target = { value: 'p2' };
        await fire('#project-select:change', { target });
        let prevented = 0;
        fire('window:beforeunload', { preventDefault: () => { prevented += 1; } });
        return { asked: [...asked], stayed: target.value, prevented, requested: requests.length };
      };
      console.log(JSON.stringify({
        consequence: contract.UNSAVED_SONG_CONTEXT_CONSEQUENCE,
        clean: await attempt({}),
        songContextOnly: await attempt({ songContextDirty: true }),
        projectOnly: await attempt({ dirty: true }),
        both: await attempt({ dirty: true, songContextDirty: true }),
      }));
    """)

    # Nothing unsaved: no question, and the switch is actually attempted.
    assert guards["clean"]["asked"] == []
    assert guards["clean"]["prevented"] == 0
    assert guards["clean"]["requested"] >= 1

    # An unsaved lyric sheet alone is enough for both guards, which is the whole finding.
    song_context = guards["songContextOnly"]
    assert len(song_context["asked"]) == 1
    assert song_context["prevented"] == 1
    assert song_context["requested"] == 0, "the switch went ahead despite the refusal"
    assert song_context["stayed"] == "p1"
    # And the question says why the Song workspace is involved, since "unsaved changes" reads as
    # the project and a Director who pasted lyrics into another panel would not connect the two.
    assert guards["consequence"] in song_context["asked"][0]
    assert "lyric sheet" in guards["consequence"]

    # An unsaved project still asks its own question, and is not told about song context it has not
    # touched -- a consequence stated for something that did not happen teaches the Director to
    # click through it.
    assert len(guards["projectOnly"]["asked"]) == 1
    assert guards["consequence"] not in guards["projectOnly"]["asked"][0]
    assert guards["projectOnly"]["prevented"] == 1
    assert guards["projectOnly"]["requested"] == 0
    assert guards["consequence"] in guards["both"]["asked"][0]


def test_a_refresh_of_the_project_on_screen_leaves_the_song_context_editors_alone():
    """`loadProject` is the refresh path as well as the switch path.

    The queue refresh, both generate paths, multiview and the batch loop all reload the project
    already on screen. Clearing the dirty flag there lets the very next render re-seed both boxes
    from the stored Song, so a sheet being pasted vanishes with nothing on screen to explain it --
    and unlike a switch, none of those actions is a decision to discard anything. A real switch does
    clear it: the Director was asked first, and the boxes belong to the project being loaded.
    """
    loads = run_workspace("""
      const song = { title: 'Spine', source: 'imported', path: 'media/songs/000-master.wav', duration: 180, lyrics: 'stored words', caption: 'stored style' };
      const typing = (id) => {
        state.project = { id, shots: [], jobs: [], song };
        state.songContextDirty = true;
        at('#song-lyrics').value = 'MID-PASTE';
      };
      typing('p1');
      answer(true);
      await fire('#refresh-jobs:click', {});
      // The render an incidental reload would have run anyway.
      app.renderSong();
      const refreshed = { dirty: state.songContextDirty, lyrics: at('#song-lyrics').value, asked: [...asked] };
      typing('p1');
      answer(true);
      await fire('#project-select:change', { target: { value: 'p2' } });
      const switched = { dirty: state.songContextDirty, asked: [...asked] };
      console.log(JSON.stringify({
        refreshed,
        switched,
        sameProject: contract.songContextSeedClearedOnLoad('p1', 'p1'),
        otherProject: contract.songContextSeedClearedOnLoad('p1', 'p2'),
        toNoProject: contract.songContextSeedClearedOnLoad('p1', ''),
        fromNoProject: contract.songContextSeedClearedOnLoad('', 'p2'),
      }));
    """)

    # A refresh of the same project: nothing asked, nothing cleared, nothing overwritten.
    assert loads["refreshed"]["asked"] == []
    assert loads["refreshed"]["dirty"] is True
    assert loads["refreshed"]["lyrics"] == "MID-PASTE"

    # A real switch asks, and then does clear: the boxes belong to the project being loaded.
    assert len(loads["switched"]["asked"]) == 1
    assert loads["switched"]["dirty"] is False

    assert loads["sameProject"] is False
    for changed in ("otherProject", "toNoProject", "fromNoProject"):
        assert loads[changed] is True, changed


def test_saving_an_emptied_song_context_asks_before_it_deletes():
    """The route assigns both fields from the body, and a Song keeps no previous version.

    Story 2.1 gave the treatment and the style bible a `*_previous` slot each; song lyrics are the
    largest hand-authored text this application accepts and have no way back, so the one save that
    cannot be undone -- replacing stored text with nothing -- is the one that asks. Replacing it
    with different text is typing, and a question on every save is a question nobody reads.
    """
    saves = run_workspace("""
      const sheet = '[Verse 1]\\n\\n    counting sodium lights\\n\\n[Chorus]\\nHold the line';
      const song = { title: 'Spine', source: 'imported', path: 'media/songs/000-master.wav', duration: 180, lyrics: sheet, caption: 'synthwave, close vocal' };
      const context = () => requests.filter((sent) => sent.path.includes('/song/context'));
      state.project = { id: 'p1', shots: [], jobs: [], song };
      state.songContextDirty = false;
      app.renderSong();
      // The Director empties the lyric box and saves.
      at('#song-lyrics').value = '';
      state.songContextDirty = true;
      answer(false);
      await fire('#save-song-context:click', {});
      const refused = { asked: [...asked], sent: context().length, dirty: state.songContextDirty };
      answer(true);
      await fire('#save-song-context:click', {});
      const accepted = { asked: [...asked], sent: context() };
      // Replacing a sheet with a different sheet is ordinary typing.
      at('#song-lyrics').value = 'A replacement sheet.';
      answer(false);
      await fire('#save-song-context:click', {});
      const replacing = { asked: [...asked], sent: context().length };
      console.log(JSON.stringify({
        refused, accepted, replacing,
        consequence: contract.SONG_CONTEXT_CLEARING_CONSEQUENCE,
        bothFields: contract.songContextClearing(song, { lyrics: '', caption: '   ' }),
        neither: contract.songContextClearing(song, { lyrics: sheet, caption: 'other words' }),
        nothingStored: contract.songContextClearing({ lyrics: '', caption: '' }, { lyrics: '', caption: '' }),
      }));
    """)

    # Refused: the question was asked, and nothing was sent.
    assert len(saves["refused"]["asked"]) == 1
    assert "lyric sheet" in saves["refused"]["asked"][0]
    assert saves["consequence"] in saves["refused"]["asked"][0]
    # The consequence used to say a song "keeps no previous version of its context". It does now,
    # so the sentence has to say what recovery there is *and* its one-step limit -- a Director told
    # the text is gone forever will not look for the Restore button that would bring it back, and a
    # Director told it is simply recoverable will not notice the next save spends the slot.
    assert "no previous version" not in saves["consequence"]
    assert "Restore" in saves["consequence"]
    assert "the next save spends it" in saves["consequence"]
    assert saves["refused"]["sent"] == 0
    # The text on screen is still the only copy, so the dirty flag must survive the refusal.
    assert saves["refused"]["dirty"] is True

    # Accepted: asked once, and then sent -- carrying the emptied field and the untouched one.
    assert len(saves["accepted"]["asked"]) == 1
    assert len(saves["accepted"]["sent"]) == 1
    sent = saves["accepted"]["sent"][0]
    assert sent["method"] == "PUT"
    assert json.loads(sent["body"]) == {"lyrics": "", "caption": "synthwave, close vocal"}

    # A replacement is not a deletion: no question, and it goes.
    assert saves["replacing"]["asked"] == []
    assert saves["replacing"]["sent"] == 1

    assert saves["bothFields"] == ["lyric sheet", "style description"]
    assert saves["neither"] == []
    assert saves["nothingStored"] == []


def song_context_controls() -> dict:
    """api.js's one table of per-field selectors, slots and restore-route path segments."""
    return run_module("""
      import { SONG_CONTEXT_CONTROLS } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify(SONG_CONTEXT_CONTROLS));
    """)


def song_context_count_boxes() -> set[str]:
    """The box selectors api.js's counter table knows about."""
    counts = run_module("""
      import { SONG_CONTEXT_COUNTS } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify(SONG_CONTEXT_COUNTS));
    """)
    return {control["field"] for control in counts}


def test_song_context_restore_controls_exist_and_agree_with_the_server():
    """A missing id breaks startup, and nothing else in the suite would notice.

    `bindEvents` dereferences both restore ids with no null check, so removing one throws before
    anything renders and the whole workspace fails to initialize. The ids, the slots they read and
    the path segment they call are read from the table app.js binds from, so a rename has to land
    in both halves -- and the slot names are checked against `Song` itself, because a slot spelled
    differently here reads `undefined` and disables a button that should have been offered.
    """
    markup = INDEX_HTML.read_text(encoding="utf-8")
    controls = song_context_controls()
    counted = song_context_count_boxes()

    assert set(controls) == set(SONG_CONTEXT_LABELS), controls
    for field, control in controls.items():
        # The path segment is the route's own, so a rename 404s rather than restoring the wrong half.
        assert control["field"] == field, control
        assert control["previousField"] == f"{field}_previous", control
        assert control["previousField"] in Song.model_fields, control["previousField"]
        assert control["label"] == SONG_CONTEXT_LABELS[field], control
        element_id = control["restore"].removeprefix("#")
        assert markup.count(f'id="{element_id}"') == 1, element_id
        # Shipped disabled: nothing is recoverable until a save has displaced something, and the
        # render is the only thing that may open it.
        assert re.search(rf'id="{element_id}"[^>]*disabled', markup), element_id
        # The box each restore belongs to is a box the workspace really has a counter for, so the
        # two tables cannot name different controls for the same field.
        assert control["box"] in counted, control
    # Two distinct buttons, or one field's restore is the other's.
    assert len({control["restore"] for control in controls.values()}) == len(controls)

    # And the route each one calls really exists on the server, built the same way the api client
    # builds it rather than spelled out here.
    call = API_JS.read_text(encoding="utf-8").split("restoreSongContext:", 1)[1].split("\n", 1)[0]
    url = re.search(r"`([^`]+)`", call)
    assert url, "api.restoreSongContext no longer builds its URL from a template literal"
    assert 'method: "POST"' in call
    # No body: the kept version lives on the server, and a client that supplied it would be
    # inventing the thing it claims to be restoring.
    assert "body:" not in call, call
    assert "JSON.stringify" not in call, call
    template = re.sub(r"\$\{[^}]+\}", "{}", url.group(1))
    template = template.replace("/{}/song/context/{}/", "/{project_id}/song/context/{field}/")
    assert template in {route.path for route in create_app().routes}, template


def test_a_song_context_restore_button_is_enabled_by_its_own_slot_including_an_empty_one():
    """The decision is executed, and the empty-versus-absent distinction is the whole point.

    `documentRestoreAvailable` tests emptiness, because a document slot is `str = ""` and cannot
    tell "kept nothing" from "kept a blank". A song slot is `str | None` precisely so it can, and
    copying the document predicate here would disable the button on exactly the case the matrix
    calls out: a Director who pasted over a blank field and wants the blank back. So `""` must
    enable and `null` must not, and a crossed wiring must never let one field's slot enable the
    other's button.
    """
    available = run_module("""
      import { songContextRestoreAvailable } from './src/music_video_producer/web/assets/api.js';
      const attempt = (fn) => { try { return fn(); } catch (error) { return `THREW: ${error.message}`; } };
      const lyricsOnly = { lyrics_previous: 'kept sheet', caption_previous: null };
      const captionOnly = { lyrics_previous: null, caption_previous: 'kept style' };
      console.log(JSON.stringify({
        keptLyrics: songContextRestoreAvailable(lyricsOnly, 'lyrics'),
        crossedToCaption: songContextRestoreAvailable(lyricsOnly, 'caption'),
        keptCaption: songContextRestoreAvailable(captionOnly, 'caption'),
        crossedToLyrics: songContextRestoreAvailable(captionOnly, 'lyrics'),
        keptBlank: songContextRestoreAvailable({ lyrics_previous: '' }, 'lyrics'),
        keptWhitespace: songContextRestoreAvailable({ lyrics_previous: '  \\n ' }, 'lyrics'),
        nullSlot: songContextRestoreAvailable({ lyrics_previous: null }, 'lyrics'),
        missing: songContextRestoreAvailable({}, 'lyrics'),
        noSong: songContextRestoreAvailable(null, 'lyrics'),
        absent: songContextRestoreAvailable(undefined, 'lyrics'),
        nonString: songContextRestoreAvailable({ lyrics_previous: 5 }, 'lyrics'),
        unknown: attempt(() => songContextRestoreAvailable({}, 'title')),
      }));
    """)

    assert available["keptLyrics"] is True
    assert available["keptCaption"] is True
    # An empty previous version is a real one: the blank a Director pasted over is recoverable.
    assert available["keptBlank"] is True
    assert available["keptWhitespace"] is True
    # A kept version of one field must not enable the other field's restore.
    assert available["crossedToCaption"] is False
    assert available["crossedToLyrics"] is False
    for nothing in ("nullSlot", "missing", "noSong", "absent", "nonString"):
        assert available[nothing] is False, nothing
    assert "THREW: Unknown song context field" in available["unknown"]


def test_rendering_the_song_sets_each_restore_button_from_its_own_stored_slot():
    """Replacing the enabled computation with a constant must not leave the suite green.

    The buttons are the entire interface to recovery. Always enabled, they offer a restore the
    route refuses with a 409 the Director did nothing to earn; never enabled, the feature does not
    exist on screen at all while every string assertion in this file still passes. So the render is
    run against the stub DOM and the buttons are read afterwards, including the case that decides
    the shape of the whole feature -- a slot holding an empty string.
    """
    rendered = run_workspace("""
      const base = { title: 'Spine', source: 'imported', path: 'media/songs/000-master.wav', duration: 180, lyrics: 'live sheet', caption: 'live style' };
      const read = () => ({
        lyrics: { disabled: at('#restore-song-lyrics').disabled, title: at('#restore-song-lyrics').title },
        caption: { disabled: at('#restore-song-style').disabled, title: at('#restore-song-style').title },
      });
      const render = (song) => { state.project = { id: 'p1', shots: [], jobs: [], song }; state.songContextDirty = false; app.renderSong(); return read(); };
      console.log(JSON.stringify({
        nothingKept: render({ ...base, lyrics_previous: null, caption_previous: null }),
        lyricsKept: render({ ...base, lyrics_previous: 'the kept sheet', caption_previous: null }),
        blankKept: render({ ...base, lyrics_previous: '', caption_previous: null }),
        bothKept: render({ ...base, lyrics_previous: 'a', caption_previous: 'b' }),
        songless: render(null),
        preRecovery: render({ ...base }),
      }));
    """)

    # Nothing displaced yet: both shut, and the tooltip says why rather than leaving a dead button.
    assert rendered["nothingKept"]["lyrics"]["disabled"] is True
    assert rendered["nothingKept"]["caption"]["disabled"] is True
    assert "No previous version" in rendered["nothingKept"]["lyrics"]["title"]

    # One field's slot opens one field's button and not the other's.
    assert rendered["lyricsKept"]["lyrics"]["disabled"] is False
    assert rendered["lyricsKept"]["caption"]["disabled"] is True
    assert "Swap the lyric sheet back" in rendered["lyricsKept"]["lyrics"]["title"]

    # The case the document predicate would have got wrong.
    assert rendered["blankKept"]["lyrics"]["disabled"] is False

    assert rendered["bothKept"]["lyrics"]["disabled"] is False
    assert rendered["bothKept"]["caption"]["disabled"] is False

    # No song at all: nothing to restore and no route to call, so both stay shut.
    assert rendered["songless"]["lyrics"]["disabled"] is True
    assert rendered["songless"]["caption"]["disabled"] is True
    # A song from a manifest written before the slots existed carries neither key.
    assert rendered["preRecovery"]["lyrics"]["disabled"] is True


def test_the_song_context_restore_click_calls_the_route_and_asks_before_discarding_typing():
    """The click is fired, not read: the handler, the guard and the request are all executed.

    Two things have to hold at once. The response re-seeds both boxes, so unsaved typing is
    discarded by a restore -- and that text was never captured, because only *stored* text becomes
    a kept version, which makes it the one thing a restore can destroy. And the request itself must
    carry no body, or the client would be supplying the very version it claims to be recovering.
    """
    clicks = run_workspace("""
      const song = { title: 'Spine', source: 'imported', path: 'media/songs/000-master.wav', duration: 180, lyrics: 'live sheet', caption: 'live style', lyrics_previous: 'the kept sheet', caption_previous: 'the kept style' };
      const restores = () => requests.filter((sent) => sent.path.includes('/song/context/'));
      const arrange = (dirty) => {
        state.project = { id: 'p1', shots: [], jobs: [], song };
        state.songContextDirty = dirty;
        app.renderSong();
      };
      arrange(false);
      answer(false);
      await fire('#restore-song-lyrics:click', {});
      const clean = { asked: [...asked], sent: restores() };
      arrange(true);
      at('#song-lyrics').value = 'MID-PASTE';
      answer(false);
      await fire('#restore-song-lyrics:click', {});
      const refused = { asked: [...asked], sent: restores().length, lyrics: at('#song-lyrics').value, dirty: state.songContextDirty };
      answer(true);
      await fire('#restore-song-style:click', {});
      const accepted = { asked: [...asked], sent: restores() };
      console.log(JSON.stringify({ clean, refused, accepted, notice: contract.songContextRestoreNotice('lyrics') }));
    """)

    # Nothing unsaved: no question, and the call goes straight out with no body.
    assert clicks["clean"]["asked"] == []
    assert len(clicks["clean"]["sent"]) == 1
    sent = clicks["clean"]["sent"][0]
    assert sent["method"] == "POST"
    assert sent["body"] is None
    assert sent["path"] == "/api/projects/p1/song/context/lyrics/restore"

    # Unsaved typing: asked, refused, and nothing sent -- the text on screen is still the only copy.
    assert len(clicks["refused"]["asked"]) == 1
    assert "discarded" in clicks["refused"]["asked"][0]
    assert clicks["refused"]["sent"] == 0
    assert clicks["refused"]["lyrics"] == "MID-PASTE"
    assert clicks["refused"]["dirty"] is True

    # Answered yes: the other field's button calls the other field's route segment.
    assert len(clicks["accepted"]["asked"]) == 1
    assert len(clicks["accepted"]["sent"]) == 1
    assert clicks["accepted"]["sent"][0]["path"].endswith("/song/context/caption/restore")

    # And the toast says the swap is reversible, or single-slot recovery nobody dares use is not
    # recovery -- the same claim the document restore's wording makes.
    assert "swaps back" in clicks["notice"]
    assert SONG_CONTEXT_LABELS["lyrics"] in clicks["notice"]


def test_song_context_restore_wording_agrees_with_the_server_on_both_sides():
    """One sentence per act, and a refusal marker that is really a substring of the server's.

    The marker is what makes the stale-project refresh work; keyed on a phrase the server does not
    actually send, the recovery path silently stops running and every retry fails identically. It
    must also share no phrase with the document refusal, or one recovery path claims the other's
    failure and refreshes a project that was never stale.
    """
    refusal = SONG_CONTEXT_RESTORE_REFUSAL.format(field=SONG_CONTEXT_LABELS["lyrics"])
    result = run_module(f"""
      import {{ SONG_CONTEXT_RESTORE_REFUSAL_MARKER, songContextRestoreNotice, songContextRestoreRefusal, songContextRestoreTitle, DOCUMENT_RESTORE_REFUSAL_MARKER }}
        from './src/music_video_producer/web/assets/api.js';
      const attempt = (fn) => {{ try {{ return fn(); }} catch (error) {{ return `THREW: ${{error.message}}`; }} }};
      console.log(JSON.stringify({{
        marker: SONG_CONTEXT_RESTORE_REFUSAL_MARKER,
        documentMarker: DOCUMENT_RESTORE_REFUSAL_MARKER,
        refusal: songContextRestoreRefusal({json.dumps(refusal)}),
        other: songContextRestoreRefusal('ComfyUI returned 400: prompt outputs failed validation'),
        missing: songContextRestoreRefusal(undefined),
        nonString: songContextRestoreRefusal(422),
        notices: {{ lyrics: songContextRestoreNotice('lyrics'), caption: songContextRestoreNotice('caption') }},
        titles: {{ available: songContextRestoreTitle('lyrics', true), empty: songContextRestoreTitle('lyrics', false) }},
        unknown: attempt(() => songContextRestoreNotice('title')),
      }}));
    """)

    assert result["marker"] in SONG_CONTEXT_RESTORE_REFUSAL
    assert result["refusal"] is True, "the predicate no longer matches the server's own refusal"
    for not_this in ("other", "missing", "nonString"):
        assert result[not_this] is False, not_this
    # The two refusals must not be confusable in either direction.
    assert result["marker"] not in DOCUMENT_RESTORE_REFUSAL
    assert result["documentMarker"] not in SONG_CONTEXT_RESTORE_REFUSAL
    # One sentence for the act, matching the server's word for word.
    for field, label in SONG_CONTEXT_LABELS.items():
        assert result["notices"][field] == SONG_CONTEXT_RESTORE_NOTICE.format(field=label), field
    assert "Swap" in result["titles"]["available"]
    assert "No previous version" in result["titles"]["empty"]
    assert "THREW: Unknown song context field" in result["unknown"]

    # A refusal must refresh the project rather than only toast, or every retry fails identically.
    handler = app_js_block("async function restoreSongContext")
    assert "api.restoreSongContext(state.project.id, field)" in handler
    assert "songContextRestoreRefusal(error.message)" in handler
    assert "api.project(" in handler
    # The dirty flag is cleared only after the server has answered, and before the re-render, or
    # the boxes keep showing the text that was just swapped out.
    assert handler.index("api.restoreSongContext(") < handler.index("state.songContextDirty = false")
    assert handler.index("state.songContextDirty = false") < handler.index("renderSong();")
    # Both buttons route through this one function, bound from the one control table.
    bindings = APP_JS.read_text(encoding="utf-8")
    assert "for (const [field, control] of Object.entries(SONG_CONTEXT_CONTROLS))" in bindings
    assert '$(control.restore).addEventListener("click", () => restoreSongContext(field));' in bindings


def test_the_song_context_editor_never_sends_a_recovery_slot():
    """Only a save that displaces something writes a slot, and only the server decides that.

    The save path builds its body from `songContextFields`, which has exactly two keys — but the
    whole-project save is the sibling write path that has twice been the hole here, and it PUTs a
    `Song` the client has been holding. Both are asserted: the context save cannot name a slot, and
    the project save cannot be the authority on one.
    """
    save = without_comments(app_js_block("async function saveSongContext()"))
    project_save = without_comments(
        APP_JS.read_text(encoding="utf-8").split("async function saveProject", 1)[1].split("\n}", 1)[0]
    )

    for slot in ("lyrics_previous", "caption_previous"):
        assert slot not in save, slot
        assert slot not in project_save, slot

    # The client may still round-trip the field it was given, so the server-side guard is what
    # actually holds -- pinned in tests/test_api.py against the route. This half only proves the
    # client never constructs one.
    body = run_module("""
      import { songContextFields } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify(Object.keys(songContextFields('words', 'style'))));
    """)
    assert body == ["lyrics", "caption"]


def test_the_song_context_boxes_count_against_the_bound_instead_of_truncating_at_it():
    """One bound, one enforcer, and four boxes that report where the text stands against it.

    These four carried `maxlength` equal to the route's limit, which is not the same behaviour as
    the route's: `maxlength` drops the tail of an oversized paste in the browser and says nothing,
    so a Director pasting a long sheet saved one ending mid-line while an API client sending the
    identical text got a 422 naming its length. The bound now belongs to the route alone -- which is
    what makes the two consistent -- and the counters are what stop the refusal being a surprise.
    """
    numbers = run_module("""
      import { SONG_CONTEXT_COUNTS, SONG_CONTEXT_LIMITS, songContextCount }
        from './src/music_video_producer/web/assets/api.js';
      const sheet = (n) => 'x'.repeat(n);
      console.log(JSON.stringify({
        counts: SONG_CONTEXT_COUNTS,
        limits: SONG_CONTEXT_LIMITS,
        under: songContextCount(sheet(SONG_CONTEXT_LIMITS.lyrics - 1), SONG_CONTEXT_LIMITS.lyrics),
        exact: songContextCount(sheet(SONG_CONTEXT_LIMITS.lyrics), SONG_CONTEXT_LIMITS.lyrics),
        over: songContextCount(sheet(SONG_CONTEXT_LIMITS.lyrics + 1), SONG_CONTEXT_LIMITS.lyrics),
        padded: songContextCount('\\n\\n  ' + sheet(SONG_CONTEXT_LIMITS.lyrics) + '  \\n\\t', SONG_CONTEXT_LIMITS.lyrics),
        empty: songContextCount(undefined, SONG_CONTEXT_LIMITS.caption),
      }));
    """)

    # The numbers the counters use are the route's own, or they report a bound nothing enforces.
    assert numbers["limits"] == {"lyrics": SONG_LYRICS_LIMIT, "caption": SONG_CAPTION_LIMIT}
    # The boundary is exactly the route's: `len(text) > limit` refuses, so `limit` itself is fine.
    assert numbers["under"]["over"] is False
    assert numbers["exact"]["over"] is False
    assert numbers["over"]["over"] is True
    # Measured after the trim, exactly as `_song_context` bounds after `.strip()` -- otherwise a
    # sheet pasted with a trailing page of newlines is reported oversized and then accepted.
    assert numbers["padded"]["over"] is False
    assert numbers["padded"]["length"] == SONG_LYRICS_LIMIT
    assert numbers["empty"]["length"] == 0
    # The verdict is in the text, not only in a colour a Director may not be looking at.
    assert "too long to save" in numbers["over"]["label"]
    assert "too long to save" not in numbers["exact"]["label"]

    markup = INDEX_HTML.read_text(encoding="utf-8")
    limits = {
        "#import-lyrics": SONG_LYRICS_LIMIT,
        "#song-lyrics": SONG_LYRICS_LIMIT,
        "#import-style": SONG_CAPTION_LIMIT,
        "#song-style": SONG_CAPTION_LIMIT,
    }
    assert {control["field"]: control["limit"] for control in numbers["counts"]} == limits

    for control in numbers["counts"]:
        field = re.search(rf'<textarea id="{control["field"][1:]}"[^>]*>', markup)
        assert field, control["field"]
        # Nothing may truncate a paste in the browser: the tail it drops is gone with no message.
        assert "maxlength" not in field.group(0), control["field"]
        # And every box has somewhere to write its count, or the count is written into nothing.
        assert f'id="{control["count"][1:]}"' in markup, control["count"]


def test_song_change_consequence_names_shot_windows_and_assembly_sync():
    """Both halves of the refusal must name what actually stops lining up.

    "Are you sure?" is not the requirement — the Director has to be told that shot
    windows are absolute seconds against the current song and that Assembly
    synchronization derives from it, on the server and in the browser alike.
    """
    script = """
      import { SONG_CHANGE_CONSEQUENCE } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({ consequence: SONG_CHANGE_CONSEQUENCE }));
    """

    browser = run_module(script)["consequence"].lower()

    for wording in ("shot window", "assembly synchronization"):
        assert wording in browser, wording
        assert wording in SONG_REPLACEMENT_CONSEQUENCE.lower(), wording
    # And it says what is *not* at risk, or the Director avoids the operation instead
    # of understanding it.
    assert "no shot data is deleted" in browser
    assert "deletes no shot data" in SONG_REPLACEMENT_CONSEQUENCE.lower()


def test_frontend_confirmation_gate_mirrors_the_server_gate():
    """Same rule on both sides: acknowledgement only once shots depend on the song."""
    script = """
      import { songChangeNeedsConfirmation } from './src/music_video_producer/web/assets/api.js';
      const song = { title: 'Spine', source: 'imported', path: 'media/songs/a.wav', duration: 180 };
      const shot = { id: 'shot_1', start: 0, duration: 5 };
      console.log(JSON.stringify({
        songAndShots: songChangeNeedsConfirmation({ song, shots: [shot] }),
        songNoShots: songChangeNeedsConfirmation({ song, shots: [] }),
        shotsNoSong: songChangeNeedsConfirmation({ song: null, shots: [shot] }),
        neither: songChangeNeedsConfirmation({ song: null, shots: [] }),
        noProject: songChangeNeedsConfirmation(null),
        absent: songChangeNeedsConfirmation(),
      }));
    """

    gate = run_module(script)

    assert gate["songAndShots"] is True
    for frictionless in ("songNoShots", "shotsNoSong", "neither", "noProject", "absent"):
        assert gate[frictionless] is False, frictionless


def test_python_and_javascript_gates_agree_on_every_project_state():
    """Both implementations of the gate, executed over the same states and compared.

    Two hand-written matrices could each be right about a different rule; this executes
    the real functions and asserts they answer identically, so the browser can never ask
    for an acknowledgement the server ignores, or stay silent where the server refuses.
    """
    script = """
      import { songChangeNeedsConfirmation } from './src/music_video_producer/web/assets/api.js';
      const song = { title: 'Spine', source: 'imported', path: 'media/songs/a.wav', duration: 180 };
      const shot = { id: 'shot_1', start: 0, duration: 5 };
      console.log(JSON.stringify({
        songAndShots: songChangeNeedsConfirmation({ song, shots: [shot] }),
        songNoShots: songChangeNeedsConfirmation({ song, shots: [] }),
        shotsNoSong: songChangeNeedsConfirmation({ song: null, shots: [shot] }),
        neither: songChangeNeedsConfirmation({ song: null, shots: [] }),
      }));
    """
    browser = run_module(script)
    project = Project(name="Gate")
    song = Song(title="Spine", source="imported", path="media/songs/a.wav", duration=180)
    shot = Shot(start=0, duration=5, prompt="Opening")
    states = {
        "songAndShots": (song, [shot]),
        "songNoShots": (song, []),
        "shotsNoSong": (None, [shot]),
        "neither": (None, []),
    }

    server = {}
    for label, (current_song, shots) in states.items():
        project.song = current_song
        project.shots = shots
        try:
            _require_song_replacement_confirmation(project, False)
            server[label] = False
        except HTTPException as error:
            assert error.status_code == 409, label
            server[label] = True
        # Acknowledgement always passes, whatever the state.
        _require_song_replacement_confirmation(project, True)

    assert server == browser
    assert server["songAndShots"] is True


def test_every_song_change_handler_confirms_before_it_sends():
    """Import, generate and remove all state the consequence before touching the server.

    Source-level because these are DOM handlers, and asserting the *order* is the point:
    a confirm after the request would be theatre. The flag sent is the Director's actual
    acknowledgement, never a hardcoded `true`, or a stale local project could defeat the
    server's gate without anyone reading the consequence.
    """
    source = APP_JS.read_text(encoding="utf-8")
    handlers = {
        "import": (
            source.split('$("#import-song").addEventListener("click"', 1)[1].split("  });", 1)[0],
            "api.uploadSong(",
        ),
        "generate": (
            source.split('musicForm.addEventListener("submit"', 1)[1].split("  });", 1)[0],
            "api.generate",
        ),
        "remove": (
            source.split('$("#remove-song").addEventListener("click"', 1)[1].split("  });", 1)[0],
            "api.removeSong(",
        ),
    }

    for label, (handler, send) in handlers.items():
        assert "confirmSongChange(" in handler, label
        assert send in handler, label
        assert handler.index("confirmSongChange(") < handler.index(send), label
        assert "change.confirmed" in handler, label
        # No spelling of a hardcoded acknowledgement, in any form. The object-literal and
        # form.append spellings are not the only ways to write it -- an assignment form is
        # already used elsewhere in this file -- so match the value, not one syntax.
        assert not re.search(r"confirm_song_replacement\W{0,4}true", handler), label

    # The shared helper is where the wording and the rule live; the handlers only ask it.
    helper = source.split("function confirmSongChange", 1)[1].split("\n}", 1)[0]
    assert "songChangeNeedsConfirmation(state.project)" in helper
    assert "SONG_CHANGE_CONSEQUENCE" in helper
    assert "window.confirm(" in helper
    # Every consequence sentence comes from the one constant, not from a handler's string.
    for wording in ("Shot windows", "Assembly synchronization"):
        assert source.count(wording) == 0, wording


def test_document_restore_wording_agrees_on_both_sides():
    """One sentence for a restore, and one for refusing it, executed on both sides.

    The Director reads the browser's toast and the thread's stored line for the same act, so
    two wordings would describe one event differently. The refusal marker is asserted to be
    a real substring of the server's own refusal, or the client's recovery path would stop
    recognising a 409 the moment the server's phrasing changed.
    """
    script = """
      import { DOCUMENT_LABELS, DOCUMENT_RESTORE_REFUSAL_MARKER, documentRestoreNotice }
        from './src/music_video_producer/web/assets/api.js';
      const attempt = (fn) => { try { return fn(); } catch (error) { return `THREW: ${error.message}`; } };
      console.log(JSON.stringify({
        labels: DOCUMENT_LABELS,
        marker: DOCUMENT_RESTORE_REFUSAL_MARKER,
        notices: {
          treatment: documentRestoreNotice('treatment'),
          style_bible: documentRestoreNotice('style_bible'),
        },
        unknown: attempt(() => documentRestoreNotice('creative_brief')),
      }));
    """

    browser = run_module(script)

    assert browser["labels"] == DOCUMENT_LABELS
    for document in DOCUMENT_LABELS:
        assert browser["notices"][document] == document_restore_notice(document), document
        # The sentence has to say the swap is reversible; single-slot recovery the Director
        # is afraid to use is not recovery.
        assert "swaps back" in browser["notices"][document], document
        # And that no model was involved, which is the point of the route existing.
        assert "No Director call was made" in browser["notices"][document], document
    assert browser["marker"] in DOCUMENT_RESTORE_REFUSAL
    # A document the server has no field for must throw rather than toast "undefined".
    assert "THREW: Unknown document" in browser["unknown"]


def test_restore_reaches_a_real_route_and_sends_no_chat_message():
    """Restore must never travel through the Director, in the client half as well.

    Asserted three ways because each could pass alone: the api client sends no body at all
    to a POST route the app really exposes, and the handler neither builds a message nor
    touches `api.directorChat`. A restore that quietly went through chat would risk a second
    unwanted rewrite while claiming to undo the first.
    """
    from music_video_producer.app import create_app

    api_source = API_JS.read_text(encoding="utf-8")
    call = api_source.split("restoreDocument:", 1)[1].split("\n", 1)[0]

    url = re.search(r"`([^`]+)`", call)
    assert url, "api.restoreDocument no longer builds its URL from a template literal"
    assert 'method: "POST"' in call
    # No body, no headers, nothing to carry a message in.
    assert "body:" not in call, call
    assert "JSON.stringify" not in call, call

    template = re.sub(r"\$\{[^}]+\}", "{}", url.group(1))
    template = template.replace("/{}/documents/{}/", "/{project_id}/documents/{document}/")
    assert template in {route.path for route in create_app().routes}, template

    handler = app_js_block("async function restoreDocument")
    assert "api.restoreDocument(state.project.id, documentKey)" in handler
    assert "documentRestoreNotice(documentKey)" in handler
    assert "directorChat" not in handler
    assert "api.saveDocuments" not in handler
    # Both controls route through this one function rather than reimplementing the call, and
    # they are bound from the one control table so a document's selector is never respelled.
    bindings = APP_JS.read_text(encoding="utf-8")
    assert "for (const [documentKey, control] of Object.entries(DOCUMENT_CONTROLS))" in bindings
    assert '$(control.restore).addEventListener("click", () => restoreDocument(documentKey));' in bindings


def test_document_save_sends_both_locks_so_the_toggles_are_not_decorative():
    """`PUT /documents` reads an absent lock as "leave it alone", by design.

    That is what stops an ordinary save from silently unlocking both documents — but it also
    means a client that omits the locks can never set one, so the save path has to send them
    every time.
    """
    handler = APP_JS.read_text(encoding="utf-8").split("async function saveProject", 1)[1].split("\n}", 1)[0]

    assert 'treatment_locked: $("#lock-treatment").checked' in handler
    assert 'style_bible_locked: $("#lock-style").checked' in handler
    # The recovery slots are never sent: only an applied Director replacement writes them.
    for forbidden in ("treatment_previous", "style_bible_previous"):
        assert forbidden not in handler, forbidden


def test_treatment_markup_exposes_every_control_the_app_dereferences():
    """A missing id breaks startup, and nothing else in the suite would notice.

    `bindEvents` runs during `init()` and dereferences all four control ids with no null
    check, so removing one throws `TypeError: Cannot read properties of null` before anything
    renders and the whole app fails to initialize. Deleting two of them left the suite green.

    The ids are read from the same table app.js binds from, so a rename has to land in both
    halves; and each pair must sit in a group scoped to its own document tab, or lock and
    restore stay visible over the Creative brief, which has neither.
    """
    markup = INDEX_HTML.read_text(encoding="utf-8")
    controls = document_controls()

    assert set(controls) == set(DOCUMENT_LABELS), controls
    tabs = re.findall(r'<button[^>]*data-doc="([^"]+)"', markup)
    for document, control in controls.items():
        assert control["tab"] in tabs, f"{document} is scoped to a tab the markup does not offer"
        group = scoped_control_group(control["tab"])
        for role in ("lock", "restore"):
            element_id = control[role].removeprefix("#")
            assert f'id="{element_id}"' in markup, f"{document}: no #{element_id} for app.js to bind"
            assert f'id="{element_id}"' in group, f"#{element_id} is not scoped to the {document} tab"
    # And the tab handler is what does the scoping, or the groups never appear at all.
    assert 'group.classList.toggle("active", group.dataset.docControls === button.dataset.doc)' in (
        APP_JS.read_text(encoding="utf-8")
    )


def test_lock_checkboxes_are_seeded_from_their_own_locked_field():
    """Losing the seeding silently unlocks both documents on the next ordinary save.

    Deleting the two seeding lines left the suite green. With them gone a locked project loads
    with both boxes unchecked, and because `PUT /documents` reads a *present* lock as an
    instruction, the next save explicitly sends `locked: false` — defeating the route's
    tri-state design from one layer up. The pairing is asserted, not just the presence: a
    crossed wiring would unlock the document the Director did not touch.
    """
    controls = document_controls()

    for document, control in controls.items():
        assert control["lockedField"] == f"{document}_locked", control
        assert control["lockedField"] in Project.model_fields, control["lockedField"]
    assert len({control["lock"] for control in controls.values()}) == len(controls), controls

    seeding = app_js_block("function syncDocumentControls")
    assert "for (const [documentKey, control] of Object.entries(DOCUMENT_CONTROLS))" in seeding
    assert "$(control.lock).checked = Boolean(state.project?.[control.lockedField]);" in seeding
    # Rendering the workspace has to seed them, or a locked project still loads unchecked.
    assert "syncDocumentControls();" in app_js_block("function renderTreatment")


def test_restore_button_enabled_state_derives_from_its_own_previous_slot():
    """Replacing the `kept` computation with `true` left the suite green.

    Both buttons are then always enabled, so a project with an empty slot offers a restore
    that 409s — and the handler misdiagnoses its own bad offer as stale state and "refreshes" a
    project that was never stale. The decision is a pure function so it can be executed here,
    including crossed cases: one document's kept version must never enable the other's button.
    """
    controls = document_controls()
    for document, control in controls.items():
        assert control["previousField"] == f"{document}_previous", control
        assert control["previousField"] in Project.model_fields, control["previousField"]

    script = """
      import { documentRestoreAvailable } from './src/music_video_producer/web/assets/api.js';
      const attempt = (fn) => { try { return fn(); } catch (error) { return `THREW: ${error.message}`; } };
      const treatmentOnly = { treatment_previous: 'kept treatment', style_bible_previous: '' };
      const styleOnly = { treatment_previous: '', style_bible_previous: 'kept style bible' };
      console.log(JSON.stringify({
        keptTreatment: documentRestoreAvailable(treatmentOnly, 'treatment'),
        crossedToStyle: documentRestoreAvailable(treatmentOnly, 'style_bible'),
        keptStyle: documentRestoreAvailable(styleOnly, 'style_bible'),
        crossedToTreatment: documentRestoreAvailable(styleOnly, 'treatment'),
        whitespace: documentRestoreAvailable({ treatment_previous: '  \\n\\t ' }, 'treatment'),
        missing: documentRestoreAvailable({}, 'treatment'),
        noProject: documentRestoreAvailable(null, 'treatment'),
        absent: documentRestoreAvailable(undefined, 'treatment'),
        nonString: documentRestoreAvailable({ treatment_previous: 5 }, 'treatment'),
        unknown: attempt(() => documentRestoreAvailable({}, 'creative_brief')),
      }));
    """

    available = run_module(script)
    assert available["keptTreatment"] is True
    assert available["keptStyle"] is True
    # A kept version of one document must not enable the other document's restore.
    assert available["crossedToStyle"] is False
    assert available["crossedToTreatment"] is False
    for empty in ("whitespace", "missing", "noProject", "absent", "nonString"):
        assert available[empty] is False, empty
    assert "THREW: Unknown document" in available["unknown"]

    # And that answer is what the button's state is, rather than a constant or a second rule.
    seeding = app_js_block("function syncDocumentControls")
    assert "const available = documentRestoreAvailable(state.project, documentKey);" in seeding
    assert "button.disabled = !available;" in seeding
    assert "documentRestoreTitle(documentKey, available)" in seeding


def test_restore_refusal_is_recognised_and_recovered_from_rather_than_just_toasted():
    """`documentRestoreRefusal` was never executed by the suite.

    Changing it to an equality check left every test green, and the predicate then never
    matches the server's real refusal sentence: the stale-project refresh silently stops
    working and every retry fails identically against the same stale state. So the predicate is
    executed against the server's own wording, an unrelated error, and a non-string.
    """
    refusal = DOCUMENT_RESTORE_REFUSAL.format(document=DOCUMENT_LABELS["treatment"])
    script = f"""
      import {{ documentRestoreRefusal, documentRestoreStaleNotice }}
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({{
        refusal: documentRestoreRefusal({json.dumps(refusal)}),
        other: documentRestoreRefusal('ComfyUI returned 400: prompt outputs failed validation'),
        missing: documentRestoreRefusal(undefined),
        nonString: documentRestoreRefusal(409),
        keptAfterRefresh: documentRestoreStaleNotice('treatment', true),
        emptyAfterRefresh: documentRestoreStaleNotice('treatment', false),
      }}));
    """

    result = run_module(script)
    assert result["refusal"] is True, "the predicate no longer matches the server's own refusal"
    assert result["other"] is False
    assert result["missing"] is False
    assert result["nonString"] is False
    # After the refresh, the refreshed project decides the wording: the refusal only proves
    # this client was stale, so a project that does hold a kept version must not be reported
    # as having none -- that tells the Director to stop trying one click short of working.
    assert "does have a kept version" in result["keptAfterRefresh"]
    assert DOCUMENT_LABELS["treatment"] in result["keptAfterRefresh"]
    assert "No kept version" in result["emptyAfterRefresh"]

    handler = app_js_block("async function restoreDocument")
    assert "documentRestoreRefusal(error.message)" in handler
    assert "api.project(" in handler, "a refusal must refresh the project, not just toast"
    assert "renderAll();" in handler
    assert (
        "documentRestoreStaleNotice(documentKey, documentRestoreAvailable(state.project, documentKey))"
        in handler
    )


def test_expansion_reaches_a_real_route_and_sends_no_chat_message_or_render():
    """Expansion is its own route, carries nothing, and queues nothing — in the client half too.

    Asserted several ways because each could pass alone: the api client POSTs to a route the app
    really exposes with no body to carry a message in, and the handler touches neither
    `api.directorChat` nor any generate call. A UI that quietly routed expansion through chat
    would apply shots positionally and could rewrite the creative documents as a side effect;
    one that queued a render would spend GPU minutes on prompts nobody has reviewed.
    """
    from music_video_producer.app import create_app

    call = API_JS.read_text(encoding="utf-8").split("expandShots:", 1)[1].split("\n", 1)[0]

    url = re.search(r"`([^`]+)`", call)
    assert url, "api.expandShots no longer builds its URL from a template literal"
    # The focus query selects the persona (story / photography) over one route.
    assert "focus=${focus}" in url.group(1)
    assert 'method: "POST"' in call
    # No body, no headers, nothing a message could travel in.
    assert "body:" not in call, call
    assert "JSON.stringify" not in call, call

    template = re.sub(r"\$\{[^}]+\}", "{project_id}", url.group(1).split("?")[0])
    assert template in {route.path for route in create_app().routes}, template

    handler = app_js_block("async function expandShotPrompts")
    assert "api.expandShots(projectId, focus)" in handler
    assert "directorChat" not in handler
    # No render is queued from here, in any spelling. The old pattern alternated on
    # `expandShots\w`, which is not a symbol that exists, so it only ever tested `api.generate`;
    # the api client is enumerated instead, which no rename or new call can slip past.
    assert re.findall(r"api\.(\w+)\(", handler) == ["expandShots"], handler
    for queues in (r"api\.generate\w*", r"generateH3", r"/generate/", r"generate/h3"):
        assert not re.search(queues, handler), (queues, handler)
    # The reply is the whole project, so the timeline and the inspector come from the response.
    assert "renderAll();" in handler
    assert "shotExpansionToast(state.project)" in handler

    source = APP_JS.read_text(encoding="utf-8")
    assert '$("#expand-shot-prompts").addEventListener("click", () => expandShotPrompts("story"));' in source
    # The DP pass binds the same handler with the photography focus (run-2 audit).
    assert '$("#dp-pass").addEventListener("click", () => expandShotPrompts("photography"));' in source

    markup = INDEX_HTML.read_text(encoding="utf-8")
    button = re.search(r'<button[^>]*id="expand-shot-prompts"[^>]*>[^<]*</button>', markup)
    assert button, "the expansion action has no button for app.js to bind"
    # It was a disabled stub; a control the Director cannot press is not an action.
    assert "disabled" not in button.group(0), button.group(0)


def test_the_expansion_control_has_one_name_in_every_layer():
    """Element id, on-screen label and handler were three different names for one control.

    `#apply-shot-plan` bound `expandShotPrompts` and rendered "Expand shots into prompts", so a
    grep for any one of them found two thirds of the feature — in a codebase whose per-document
    controls are driven from a single table precisely so a rename cannot half-land.
    """
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")

    button = re.search(r'<button[^>]*id="expand-shot-prompts"[^>]*>([^<]*)</button>', markup)
    assert button, "the expansion action has no button for app.js to bind"
    assert button.group(1).strip() == "Expand shots into prompts", button.group(1)

    # One spelling, everywhere it is reachable: the markup id, the binding, and the
    # handler's focus ternary (the DP pass shares the handler, so the button lookup
    # carries both ids in one expression).
    assert source.count('$("#expand-shot-prompts")') == 1, source.count('$("#expand-shot-prompts")')
    assert '$(focus === "photography" ? "#dp-pass" : "#expand-shot-prompts")' in source
    assert 'async function expandShotPrompts(focus = "story")' in source
    # And the old name is gone from every layer, including the stylesheet.
    for layer, text in (("markup", markup), ("app.js", source), ("styles.css", styles)):
        assert "apply-shot-plan" not in text, layer


def test_expansion_wording_is_one_constant_per_layer_rather_than_hand_written_twice():
    """Two sentences for one rule drift, and the button and the toast already disagreed.

    The empty-plan refusal exists on both sides — the server's 422 and the client's pre-emptive
    toast — and the "nothing is rendered" claim exists on both halves of the control, where the
    tooltip said "Nothing is rendered" and the toast said "Nothing was rendered" about the same
    act. Each is one constant now, and this is what keeps the copies the markup and the server
    cannot import agreeing with it.
    """
    from music_video_producer.app import EXPANSION_WITHOUT_SHOTS

    shared = run_module("""
      import { SHOT_EXPANSION_NO_RENDER, SHOT_EXPANSION_TOAST, SHOT_EXPANSION_WITHOUT_SHOTS }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        noRender: SHOT_EXPANSION_NO_RENDER,
        toast: SHOT_EXPANSION_TOAST,
        withoutShots: SHOT_EXPANSION_WITHOUT_SHOTS,
      }));
    """)

    # One refusal for one rule: the browser refuses before sending exactly what the route refuses.
    assert shared["withoutShots"] == EXPANSION_WITHOUT_SHOTS
    # And the handler states it from the constant rather than writing its own sentence.
    handler = app_js_block("async function expandShotPrompts")
    assert "SHOT_EXPANSION_WITHOUT_SHOTS" in handler
    assert "add shots to the timeline" not in without_comments(handler).lower()

    # The claim the Director reads before pressing and the one they read afterwards are the same
    # spelling; the markup cannot import the constant, so this is what holds them together.
    markup = INDEX_HTML.read_text(encoding="utf-8")
    button = re.search(r'<button[^>]*id="expand-shot-prompts"[^>]*>[^<]*</button>', markup)
    assert button, "the expansion action has no button for app.js to bind"
    assert shared["noRender"] in button.group(0), button.group(0)
    assert shared["noRender"] in shared["toast"], shared["toast"]
    # No second tense of the same claim anywhere in the two files that carry it.
    assert "Nothing was rendered" not in markup
    assert "Nothing was rendered" not in API_JS.read_text(encoding="utf-8")


def test_expansion_toast_is_decided_by_the_reply_rather_than_by_a_diff():
    """A re-run made the loudest thing on screen contradict the reply directly beside it.

    The toast was diff-derived, so expanding twice against a model that returns the same prompts
    announced "No shot prompt changed" under a server notice saying prompts were written for two
    shots. The count now comes out of the reply itself — keyed on the server's own
    EXPANSION_WRITTEN_NOTICE, read back out of a really-formatted one — so the two cannot disagree.
    """
    from music_video_producer.app import (
        EXPANSION_LOCKED_NOTICE,
        EXPANSION_OMITTED_NOTICE,
        EXPANSION_WRITTEN_NOTICE,
    )

    labels = ["shot 01 at 0s (shot_first)", "shot 02 at 5s (shot_second)"]
    written_two = EXPANSION_WRITTEN_NOTICE.format(count=2, shots=", ".join(labels))
    written_one = EXPANSION_WRITTEN_NOTICE.format(count=1, shots=labels[0])
    locked = EXPANSION_LOCKED_NOTICE.format(shots=", ".join(labels))
    omitted = EXPANSION_OMITTED_NOTICE.format(shots=", ".join(labels))
    script = f"""
      import {{ SHOT_EXPANSION_WRITTEN_MARKER, shotExpansionToast, shotExpansionWritten }}
        from './src/music_video_producer/web/assets/api.js';
      const notices = {json.dumps({
        "writtenTwo": written_two,
        "writtenOne": written_one,
        "locked": locked,
        "omitted": omitted,
    })};
      const reply = (...contents) => ({{ messages: [
        {{ role: 'user', content: 'Expand the shots.' }},
        ...contents.map((content) => ({{ role: 'assistant', content }})),
      ] }});
      const message = (...parts) => 'Here is the expansion.\\n\\n---\\n' + parts.join('\\n\\n');
      console.log(JSON.stringify({{
        marker: SHOT_EXPANSION_WRITTEN_MARKER,
        count: shotExpansionWritten(reply(message(notices.writtenTwo))),
        // The re-run: identical prompts, and the reply still reports them as written.
        reRun: shotExpansionToast(reply(message(notices.writtenTwo))),
        one: shotExpansionToast(reply(message(notices.writtenOne))),
        withOtherNotices: shotExpansionToast(reply(message(notices.writtenOne, notices.locked))),
        // Nothing written: every one of these leaves the prompts as they were.
        lockedOnly: shotExpansionToast(reply(message(notices.locked))),
        omittedOnly: shotExpansionToast(reply(message(notices.omitted))),
        // An earlier expansion's notice is still in the thread; the last reply decides.
        onlyEarlier: shotExpansionToast(reply(message(notices.writtenTwo), 'Nothing to change.')),
        // Model prose is rendered above the notices in the same message, so the match is the
        // server's whole notice shape rather than a phrase prose could plausibly contain.
        prose: shotExpansionToast(reply('Prompts written for every shot I could see, I think.')),
        systemOnly: shotExpansionToast({{ messages: [
          {{ role: 'system', content: message(notices.writtenTwo) }},
        ] }}),
        noMessages: shotExpansionToast({{ messages: [] }}),
        noProject: shotExpansionToast(null),
        absent: shotExpansionToast(),
        nonArray: shotExpansionToast({{ messages: 'nope' }}),
      }}));
    """

    toasts = run_module(script)
    assert toasts["marker"] in written_two, "the toast no longer reads the server's own notice"
    assert toasts["count"] == 2
    assert toasts["reRun"].startswith("2 shot prompts written")
    assert toasts["one"].startswith("1 shot prompt written")
    assert toasts["withOtherNotices"].startswith("1 shot prompt written")
    for unchanged in ("lockedOnly", "omittedOnly", "onlyEarlier", "prose", "systemOnly",
                      "noMessages", "noProject", "absent", "nonArray"):
        assert "No shot prompt changed" in toasts[unchanged], unchanged
    # Both things a Director watching a "Director" button needs to know.
    assert "Nothing is rendered" in toasts["reRun"]
    assert "editable in the shot inspector" in toasts["reRun"]

    # The handler hands it the response, not a diff: a live `state.project.shots` reference is
    # mutated by an inspector or timeline edit made during the call, so it could not be diffed
    # against anyway.
    handler = app_js_block("async function expandShotPrompts")
    assert "const before" not in handler, handler
    assert "shotExpansionToast(state.project)" in handler


def test_expansion_shuts_out_the_silent_shot_saves_that_would_revert_it():
    """`await shotSaveChain` was unpinned, and on its own it is not enough.

    Deleting it left the whole suite green while a drag followed immediately by a press let the
    stale whole-list save land *after* the expansion and wipe every prompt just written — with the
    success toast on screen and the reply claiming they were written. Awaiting only drains what was
    pending at click time, so a drag *during* the multi-second call queues the same stale save; the
    in-flight flag is what closes that, and it has to be set before the await rather than after.
    """
    source = APP_JS.read_text(encoding="utf-8")
    handler = app_js_block("async function expandShotPrompts")
    saver = app_js_block("function saveShotsSilently")

    # Half one: saves queued before the click are drained before the request is sent.
    assert "await shotSaveChain;" in handler
    assert handler.index("await shotSaveChain") < handler.index("api.expandShots("), handler

    # Half two: no new save may be queued for the duration, so the flag goes up before the drain
    # and comes down in `finally`, where a failed or refused expansion also releases it.
    assert handler.index('shotWriteInFlight = "expansion";') < handler.index("await shotSaveChain")
    assert RELEASED_IN_FINALLY.search(handler), handler
    # The flag is shared with Assistant ProducerBot, which needs exactly the same protection and
    # would otherwise get a second copy of it. Two *names* and only two, so a third path silently
    # blocking every timeline save under a name nothing explains still fails here -- which is what
    # this assertion was written to catch.
    #
    # Four raisers under those two names since pass two landed: the pass-one expansion, the per-shot
    # H3 expansion, the whole-plan H3 sweep, and the assistant fill. All three expansion writers
    # revert the same work in the same way and correctly say the same sentence about it, which is
    # exactly the sharing the string was introduced for -- what the wording distinguishes is a fill
    # from an expansion, not one expansion route from another.
    raisers = re.findall(r'shotWriteInFlight = "(\w+)"', source)
    assert sorted(raisers) == ["assistant", "expansion", "expansion", "expansion"], raisers
    # Released in a `finally` by each of them, so a failed or refused write does not wedge every
    # timeline save off for the life of the page.
    assert len(RELEASED_IN_FINALLY.findall(source)) == len(raisers), source

    # The refusal lives in the one function every silent save goes through, ahead of both the
    # queueing and the dirty flags -- a save that is refused was never pending.
    assert "shotWriteInFlight" in saver
    assert "SHOT_EXPANSION_EDIT_BLOCKED" in saver
    assert "ASSISTANT_EDIT_BLOCKED" in saver
    for later in ("state.shotsDirty = true;", "shotSaveChain = shotSaveChain", "api.saveShots("):
        assert saver.index("shotWriteInFlight") < saver.index(later), later
    # And it is said out loud: the edit really is not saved, and the response re-renders the
    # timeline over it, so a drag that silently vanishes reads as the app losing work at random.
    # Both wordings, because a refusal that names the wrong write is one the Director cannot act on.
    blocked = run_module("""
      import { ASSISTANT_EDIT_BLOCKED, SHOT_EXPANSION_EDIT_BLOCKED }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        blocked: SHOT_EXPANSION_EDIT_BLOCKED, assistant: ASSISTANT_EDIT_BLOCKED,
      }));
    """)
    for wording in (blocked["blocked"].lower(), blocked["assistant"].lower()):
        assert "not saved" in wording
        assert "again" in wording, wording
    assert blocked["blocked"] != blocked["assistant"]

    # Every timeline mutation goes through that one function rather than calling the route itself.
    #
    # Two senders and exactly two. `stepHistory` -- the undo/redo write -- cannot go through
    # `saveShotsSilently`, because it has to carry the revision the history stack is valid against
    # rather than whatever this client last saw, and has to adopt the reply's shots rather than
    # discard them. So it is held to the same two protections right here, which makes the
    # exemption a narrower rule rather than a hole: the same in-flight flag, and a drained save
    # chain before the request.
    stepper = app_js_block("async function stepHistory")
    assert "api.saveShots(" not in source.replace(saver, "").replace(stepper, ""), (
        "a shot save bypasses both saveShotsSilently and the undo write"
    )
    assert "api.saveShots(projectId, entry.shots, undoRevision)" in stepper, stepper
    assert "busy: shotWriteInFlight" in stepper, stepper
    assert stepper.index("await shotSaveChain") < stepper.index("api.saveShots("), stepper


def test_expansion_abandons_a_result_for_a_project_that_is_no_longer_loaded():
    """`state.project = await api.expandShots(...)` reassigned unconditionally after a long await.

    Only the button is disabled during the call — the project selector stays live — so switching
    projects while the model thinks let project A's result be written over project B, and
    `renderAll()` then drew A's shots and A's documents under B's name. Nothing is lost by dropping
    it: the prompts are saved on the server, and loading that project again shows them.
    """
    handler = app_js_block("async function expandShotPrompts")

    # The id is captured before any await, and it is the id the request is sent for.
    assert "const projectId = state.project.id;" in handler
    assert handler.index("const projectId") < handler.index("await ")
    assert "api.expandShots(projectId, focus)" in handler
    # The response is held aside until the guard has run, so a stale result is never assigned.
    assert "if (state.project?.id !== projectId) return;" in handler
    assert handler.index("api.expandShots(") < handler.index("state.project?.id !== projectId")
    assert handler.index("state.project?.id !== projectId") < handler.index("state.project = expanded")
    for applied in ("renderAll();", "shotExpansionToast(", "markDocumentsSaved();"):
        assert handler.index("state.project?.id !== projectId") < handler.index(applied), applied


def test_chat_toast_reports_what_changed_instead_of_asserting_an_update():
    """The reply states what actually changed; the toast is the loudest thing on screen.

    It used to claim "Treatment updated" unconditionally -- including when the document was
    locked, when the guard rejected the candidate, and when the rewrite was identical. Derived
    from the project before and after the call, so it cannot contradict the reply it sits next to.
    """
    script = """
      import { documentChangeToast } from './src/music_video_producer/web/assets/api.js';
      const before = { treatment: 'old treatment', style_bible: 'old style bible' };
      console.log(JSON.stringify({
        treatmentOnly: documentChangeToast(before, { ...before, treatment: 'new treatment' }),
        styleOnly: documentChangeToast(before, { ...before, style_bible: 'new style bible' }),
        both: documentChangeToast(before, { treatment: 'new t', style_bible: 'new s' }),
        lockedOrRejected: documentChangeToast(before, { ...before }),
        identicalRewrite: documentChangeToast(before, { ...before }),
        firstEverFill: documentChangeToast({}, { treatment: 'first treatment' }),
      }));
    """

    toasts = run_module(script)
    assert "Treatment" in toasts["treatmentOnly"]
    assert "Style bible" not in toasts["treatmentOnly"]
    assert "Style bible" in toasts["styleOnly"]
    assert "Treatment" not in toasts["styleOnly"]
    for label in DOCUMENT_LABELS.values():
        assert label in toasts["both"], label
    # Nothing moved: the toast must say so rather than announce an update that never happened.
    for unchanged in ("lockedOrRejected", "identicalRewrite"):
        assert "no document changed" in toasts[unchanged], unchanged
        for label in DOCUMENT_LABELS.values():
            assert label not in toasts[unchanged], (unchanged, label)
    assert "Treatment" in toasts["firstEverFill"]

    handler = chat_submit_handler()
    assert "const before = state.project;" in handler
    assert "documentChangeToast(before, state.project" in handler
    # The old unconditional claim is gone from the whole module, in any handler.
    assert "Treatment updated" not in APP_JS.read_text(encoding="utf-8")


def test_composer_sends_document_consent_from_its_own_control():
    """Off by default, per turn, and read from the checkbox rather than hardcoded.

    A fixed `true` reinstates the unrequested rewrite the flag exists to stop, and a fixed
    `false` makes the control decorative -- either mutation leaves every route test green,
    because the server stays perfectly correct about a flag the browser never sends honestly.
    """
    markup = INDEX_HTML.read_text(encoding="utf-8")
    shared = document_opt_in()

    # Consent belongs to the turn being sent, so the control lives in the composer rather than
    # the document actions row, which is per-document and persistent.
    composer = re.search(r'<form class="composer" id="chat-form">.*?</form>', markup, re.DOTALL)
    assert composer, "the chat composer form is no longer where app.js binds it"
    element_id = shared["control"].removeprefix("#")
    checkbox = re.search(rf'<input type="checkbox" id="{element_id}"[^>]*>', composer.group(0))
    assert checkbox, f"no #{element_id} checkbox in the chat composer for app.js to read"
    # Nothing remembers consent, so the markup must not preset it either.
    assert "checked" not in checkbox.group(0), checkbox.group(0)

    # The markup's own wording is the one both notices quote; the markup cannot import the
    # constant, so this test is what keeps the three copies agreeing.
    text = " ".join(re.sub(r"<[^>]+>", " ", composer.group(0)).split())
    assert shared["label"] == APPLY_DOCUMENTS_LABEL
    assert shared["label"] in text, text

    # And the row wraps: it carries three items in the workspace's narrow column, and with
    # nowrap the control that has to be readable to be consent is the one that squeezes.
    composer_row = re.search(r"\.composer > div \{([^}]*)\}", STYLES_CSS.read_text(encoding="utf-8"))
    assert composer_row, "the composer's control row no longer has a layout rule"
    assert "flex-wrap: wrap" in composer_row.group(1), composer_row.group(1)

    handler = chat_submit_handler()
    assert "const applyDocuments = documentConsent(applyDocumentsControl());" in handler
    assert "apply_documents: applyDocuments" in handler
    # No spelling of a hardcoded consent, in any form -- match the value, not one syntax.
    assert not re.search(r"apply_documents\W{0,4}(true|false)", handler), handler


def test_consent_is_read_as_a_decline_when_the_control_is_missing():
    """The send handler reads the consent *outside* the try/catch that reports failures.

    A bare `.checked` on a missing control therefore throws past the handler and kills the send
    with no request, no toast and no error -- the control silently deciding to send nothing at
    all. A control that is not there has given no consent, so absence must read as a decline.
    """
    script = """
      import { documentConsent } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        ticked: documentConsent({ checked: true }),
        unticked: documentConsent({ checked: false }),
        missing: documentConsent(null),
        absent: documentConsent(),
        // `checked` is a real boolean on a real checkbox; anything else is not consent.
        truthy: documentConsent({ checked: "yes" }),
        noProperty: documentConsent({}),
      }));
    """

    consent = run_module(script)
    assert consent["ticked"] is True
    for declined in ("unticked", "missing", "absent", "truthy", "noProperty"):
        assert consent[declined] is False, declined

    # And the one place that turns the selector into an element is where the handler gets it.
    assert "$(APPLY_DOCUMENTS_CONTROL)" in app_js_block("function applyDocumentsControl")


def test_consent_is_cleared_after_every_turn_and_on_every_project_change():
    """Without this the feature's headline claim is false: it is on until the Director notices.

    Nothing else unchecks the box, so one tick applies every later turn with no fresh consent,
    and the box survives a project switch -- so consent given in one project is inherited by the
    next one loaded, replacing a document in a project nobody was looking at.

    The round trip is *executed* rather than grepped, because the mutation that matters keeps
    every identifier and simply does not clear: reading the real control back through the real
    reader is the only thing that proves the second turn sends `false`.
    """
    script = """
      import { clearDocumentConsent, documentConsent }
        from './src/music_video_producer/web/assets/api.js';
      const control = { checked: false };
      const trail = [];
      // Turn one: the Director ticks the box and sends.
      control.checked = true;
      trail.push(documentConsent(control));
      clearDocumentConsent(control);
      // Turn two: nothing was ticked in between, so it must send a decline.
      trail.push(documentConsent(control));
      // Clearing an already-clear control, and a control that is not there at all, are both
      // no-ops rather than throws -- the project-load path runs before any markup is required.
      clearDocumentConsent(control);
      const survived = (() => {
        try { clearDocumentConsent(null); clearDocumentConsent(); return true; }
        catch (error) { return `THREW: ${error.message}`; }
      })();
      console.log(JSON.stringify({ trail: [...trail, documentConsent(control)], survived }));
    """

    lifecycle = run_module(script)
    assert lifecycle["trail"] == [True, False, False]
    assert lifecycle["survived"] is True

    source = APP_JS.read_text(encoding="utf-8")
    # Cleared in `finally`, so a failed send spends the consent too: the turn is over either
    # way, and leaving it ticked after an error is the same "on until noticed" behaviour.
    handler = chat_submit_handler()
    assert "finally { clearDocumentConsent(applyDocumentsControl());" in handler
    assert handler.index("api.directorChat(") < handler.index("clearDocumentConsent(")

    # Ahead of loadProject's no-project branch, so switching *away* clears it too rather than
    # leaving it ticked for whichever project is loaded next. Anchored on the signature, not the
    # name: `loadProjects` is a different function and would otherwise be the one this reads.
    load = source.split("async function loadProject(id) {", 1)[1].split("\n}", 1)[0]
    assert "clearDocumentConsent(applyDocumentsControl());" in load
    assert load.index("clearDocumentConsent(") < load.index("if (!id)")
    # And it is a change of project that clears it, not every load -- see
    # test_a_same_project_refresh_does_not_revoke_consent_the_director_just_gave.
    assert "documentConsentClearedOnLoad(state.project?.id, id)" in load

    # Exactly these two callers; a third would be a rule nobody stated, and nothing anywhere
    # ticks the box from code -- consent comes from the Director or not at all.
    assert source.count("clearDocumentConsent(") == 2
    assert not re.search(r"apply-documents\"\)\.checked = true|Control\(\)\.checked = true", source)


def test_a_same_project_refresh_does_not_revoke_consent_the_director_just_gave():
    """`loadProject` is the refresh path as well as the switch path.

    Most of its call sites reload the project already on screen — the queue refresh, both generate
    paths, multiview and the queue-ready loop — so clearing on every load unticked a box the
    Director had ticked seconds ago, in the project they were still looking at, with nothing on
    screen to explain it. The direction is safe, but "consent is per project" is what the feature
    claims and per-refresh is a different rule.

    The decision is a pure function so the refresh case can be executed rather than grepped, and
    the call sites are read off the source so which loads are refreshes is pinned rather than
    assumed.
    """
    decisions = run_module("""
      import { documentConsentClearedOnLoad } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        switched: documentConsentClearedOnLoad('project_a', 'project_b'),
        refreshed: documentConsentClearedOnLoad('project_a', 'project_a'),
        leaving: documentConsentClearedOnLoad('project_a', ''),
        arriving: documentConsentClearedOnLoad(null, 'project_a'),
        firstEver: documentConsentClearedOnLoad(undefined, 'project_a'),
        neither: documentConsentClearedOnLoad(null, ''),
        absent: documentConsentClearedOnLoad(),
      }));
    """)

    # The whole point: reloading the same project is not a change of project.
    assert decisions["refreshed"] is False
    # Nor is arriving at no project from no project; there is nothing to inherit either way.
    assert decisions["neither"] is False
    assert decisions["absent"] is False
    # Leaving *and* arriving both count, or consent given in one project is inherited by the next
    # one loaded -- including via the no-project branch.
    for cleared in ("switched", "leaving", "arriving", "firstEver"):
        assert decisions[cleared] is True, cleared

    source = APP_JS.read_text(encoding="utf-8")
    # Every call site, read off the source: the declaration, the refreshes that reload the project
    # already on screen, and the two that can actually change project.
    arguments = [
        argument for argument in re.findall(r"loadProject\(([^)]*)\)", source) if argument != "id"
    ]
    # `projectId` is the batch handler's own capture of `state.project.id`, taken before its first
    # await, and every load through it sits behind the guard that abandons a batch whose project
    # was switched away from -- so it can only ever reload the project already on screen.
    reloads = {"state.project.id", "projectId"}
    refreshes = [argument for argument in arguments if argument in reloads]
    switches = [argument for argument in arguments if argument not in reloads]
    assert len(refreshes) >= 5, arguments
    assert "const projectId = state.project.id;" in queue_handler_body()
    # A switch is the selector's own value, or the id `loadProjects` resolved; anything else is a
    # call site nobody classified, and it would silently pick one of the two behaviours.
    assert set(switches) == {"next", "event.target.value"}, switches


def test_the_send_confirmation_describes_the_send_that_is_actually_happening():
    """Both sends re-render the editors from the server, so the gate fires either way.

    But only a consented send can *replace* a document, and warning that "a reply can replace
    either creative document" when the box is unchecked deters a send that would write nothing.
    """
    question = app_js_block("function directorSendQuestion")
    assert "applyDocuments" in question
    consented, declined = question.split("?", 1)[1].split(":", 1)
    assert "replace either creative document" in consented
    assert "replace" not in declined.replace("No document will be replaced", "")
    # The declined sentence still states the loss the dialog exists for, or the Director reads
    # "nothing will happen" and clicks through the one question that protects unsaved typing.
    assert "re-rendered from the text stored on the server" in declined

    handler = chat_submit_handler()
    assert "confirmDiscardingDocumentEdits(directorSendQuestion(applyDocuments))" in handler
    # The gate is not skipped for a declined turn: the editors are overwritten regardless.
    assert handler.index("directorSendQuestion") < handler.index("api.directorChat(")


def test_document_opt_in_wording_agrees_on_both_sides():
    """One sentence per side for a declined turn, mirrored.

    The thread line names *which* documents were proposed; the toast is the loudest thing on
    screen and says why none of them landed. Two independently written wordings would describe
    one event differently, and either could quietly stop naming the control that applies it.
    """
    notice = document_not_requested_notice([DOCUMENT_LABELS["treatment"]])
    browser = document_opt_in()["toast"]

    for wording in (APPLY_DOCUMENTS_LABEL, "opt-in per turn"):
        assert wording in notice, wording
        assert wording in browser, wording
    # The server's half names the documents, which is the half the client cannot know.
    assert DOCUMENT_LABELS["treatment"] in notice
    # Neither side claims a write, and neither promises recovery for text that was never
    # stored -- a declined proposal is not kept anywhere.
    for claim in ("Replaced by this reply", "can be restored"):
        assert claim not in notice, claim
        assert claim not in browser, claim
    assert "not kept" in notice
    assert "not kept" in browser


def test_a_declined_turn_toast_explains_the_opt_in_instead_of_claiming_a_change():
    """"No document changed" is true of a declined turn and useless.

    Nothing changed for a reason the Director controls, so the toast has to say which box to
    tick -- while a lock or a rejection must keep the vaguer sentence, because sending the
    Director to tick a box that was already ticked points them at the wrong thing.

    Every declined case is decided by the *reply*, not by the diff, and the replies here are
    built from the server's own sentences rather than hand-copied ones, so a change to either
    wording lands in this test instead of silently unhooking the toast from the server.
    """
    declined = document_not_requested_notice(list(DOCUMENT_LABELS.values()))
    locked = DOCUMENT_LOCK_NOTICE.format(document=DOCUMENT_LABELS["treatment"])
    replies = {
        # A real proposal the opt-in is what stopped: the one case that may blame the flag.
        "proposed": declined,
        # The server is silent when nothing was proposed, when the candidate was an echo, and
        # when the guard would have refused it anyway. Blaming the flag for any of those sends
        # the Director to tick a box and retry a turn that writes nothing either way.
        "nothingProposed": "Nothing to change.",
        # And a locked document carries the lock's sentence, not the opt-in's: ticking the box
        # would not apply it.
        "lockedOnly": f"Nothing to change.\n\n---\n{locked}",
    }
    script = f"""
      import {{ documentChangeToast }} from './src/music_video_producer/web/assets/api.js';
      const replies = {json.dumps(replies)};
      const reply = (content) => ({{ messages: [
        {{ role: 'user', content: 'Anything to add?' }},
        {{ role: 'assistant', content }},
      ] }});
      const documents = {{ treatment: 'old treatment', style_bible: 'old style bible' }};
      const project = (content, overrides = {{}}) =>
        ({{ ...documents, ...overrides, ...reply(content) }});
      console.log(JSON.stringify({{
        declined: documentChangeToast(documents, project(replies.proposed), false),
        declinedNothingProposed: documentChangeToast(documents, project(replies.nothingProposed), false),
        declinedLockedOnly: documentChangeToast(documents, project(replies.lockedOnly), false),
        declinedAfterConcurrentRestore: documentChangeToast(
          documents, project(replies.proposed, {{ treatment: 'the restored treatment' }}), false),
        declinedNoMessages: documentChangeToast(documents, {{ ...documents }}, false),
        lockedOrRejected: documentChangeToast(documents, project(replies.lockedOnly), true),
        applied: documentChangeToast(documents, project('Done.', {{ treatment: 'new treatment' }}), true),
        defaulted: documentChangeToast(documents, project(replies.proposed)),
      }}));
    """

    toasts = run_module(script)
    assert "opt-in per turn" in toasts["declined"]
    assert APPLY_DOCUMENTS_LABEL in toasts["declined"]
    # It must not claim a document moved, in either direction.
    for label in DOCUMENT_LABELS.values():
        assert label not in toasts["declined"], label
    assert "replaced by this reply" not in toasts["declined"].lower()

    # The server's silence, mirrored: no proposal to apply means no box to tick.
    for silent in ("declinedNothingProposed", "declinedLockedOnly", "declinedNoMessages"):
        assert "opt-in per turn" not in toasts[silent], silent
        assert APPLY_DOCUMENTS_LABEL not in toasts[silent], silent
        assert "no document changed" in toasts[silent], silent

    # A restore or a hand edit committed while the Director call was in flight moves the
    # document without this reply having touched it. Consent says the reply cannot have written
    # anything, so the diff must not be allowed to credit it with a replacement.
    assert toasts["declinedAfterConcurrentRestore"] == toasts["declined"]
    for label in DOCUMENT_LABELS.values():
        assert label not in toasts["declinedAfterConcurrentRestore"], label

    # Consent was given here: the flag is not what stopped it.
    assert "opt-in per turn" not in toasts["lockedOrRejected"]
    assert "no document changed" in toasts["lockedOrRejected"]
    # An applied change still reports the change.
    assert DOCUMENT_LABELS["treatment"] in toasts["applied"]
    # A caller that omits the consent gets the diff-derived sentence, never the one that blames
    # the flag for a lock or a rejection.
    assert toasts["defaulted"] == toasts["lockedOrRejected"]

    assert "documentChangeToast(before, state.project, applyDocuments)" in chat_submit_handler()


def test_the_declined_marker_is_a_real_substring_of_the_servers_own_notice():
    """The toast's one input the project cannot supply, keyed like every other marker here.

    `documentProposalDeclined` is what stops the toast blaming the opt-in for the three cases
    the server deliberately says nothing about. Changing it to an equality check, or letting
    the server's phrasing move, leaves the predicate matching nothing -- and every declined turn
    silently reverts to the vaguer sentence with no test noticing.
    """
    notice = document_not_requested_notice([DOCUMENT_LABELS["treatment"]])
    script = f"""
      import {{ DOCUMENT_NOT_REQUESTED_MARKER, documentProposalDeclined }}
        from './src/music_video_producer/web/assets/api.js';
      const reply = (content) => ({{ messages: [{{ role: 'assistant', content }}] }});
      console.log(JSON.stringify({{
        marker: DOCUMENT_NOT_REQUESTED_MARKER,
        declined: documentProposalDeclined(reply({json.dumps(notice)})),
        other: documentProposalDeclined(reply('Nothing to change.')),
        // The *last* assistant line decides: an earlier declined turn is still in the thread.
        onlyEarlier: documentProposalDeclined({{ messages: [
          {{ role: 'assistant', content: {json.dumps(notice)} }},
          {{ role: 'user', content: 'and now?' }},
          {{ role: 'assistant', content: 'Nothing to change.' }},
        ] }}),
        // A system line is the restore audit trail, never a Director reply.
        systemOnly: documentProposalDeclined({{ messages: [
          {{ role: 'system', content: {json.dumps(notice)} }},
        ] }}),
        noMessages: documentProposalDeclined({{ messages: [] }}),
        noProject: documentProposalDeclined(null),
        absent: documentProposalDeclined(),
        nonArray: documentProposalDeclined({{ messages: 'nope' }}),
      }}));
    """

    result = run_module(script)
    assert result["marker"] in notice, "the predicate no longer matches the server's own notice"
    assert result["declined"] is True
    assert result["other"] is False
    assert result["onlyEarlier"] is False
    assert result["systemOnly"] is False
    for empty in ("noMessages", "noProject", "absent", "nonArray"):
        assert result[empty] is False, empty


def test_editor_overwrites_warn_before_discarding_unsaved_edits_and_clear_the_flags():
    """Unsaved textarea edits are the one loss this feature cannot undo.

    A restore, a Director reply and a shot expansion all re-render the editors from the server,
    and the captured "previous version" is the *stored* text -- so on-screen edits are
    unrecoverable. Every such path asks first, matching the `window.confirm` precedent on project
    switch, and then clears the dirty flags exactly as `saveProject` does: text that matches the
    server is not dirty, and a project permanently flagged dirty teaches the Director to click
    through the one question that protects real work.

    Expansion is in this list because it is the path that reached `renderAll()` with no gate at
    all: the button sits in the document-actions row beside the editors, so "type into the
    Treatment, then press Expand" is an ordinary gesture, and it discarded the typing silently
    while leaving the dirty flag set over textareas that had just been overwritten.
    """
    guard = app_js_block("function confirmDiscardingDocumentEdits")
    assert "state.documentsDirty" in guard
    assert "window.confirm(" in guard
    assert "UNSAVED_DOCUMENT_EDITS_CONSEQUENCE" in guard

    source = APP_JS.read_text(encoding="utf-8")
    handlers = {
        "restore": (app_js_block("async function restoreDocument"), "api.restoreDocument("),
        "chat": (
            source.split('$("#chat-form").addEventListener("submit"', 1)[1].split("  });", 1)[0],
            "api.directorChat(",
        ),
        "expand": (app_js_block("async function expandShotPrompts"), "api.expandShots("),
    }
    for label, (handler, send) in handlers.items():
        assert "confirmDiscardingDocumentEdits(" in handler, label
        # Asked *before* the request, or the warning is theatre over an already-lost edit.
        assert handler.index("confirmDiscardingDocumentEdits(") < handler.index(send), label
        # Every re-render from the server clears the flags, asserted per re-render: the restore
        # handler has two such paths, and clearing on only one leaves the project flagged dirty
        # against text that already matches the server.
        before_rerenders = handler.split("renderAll();")[:-1]
        assert before_rerenders, label
        for index, segment in enumerate(before_rerenders):
            assert segment.rstrip().endswith("markDocumentsSaved();"), (label, index)

    saved = app_js_block("function markDocumentsSaved")
    assert "state.documentsDirty = false;" in saved
    assert "state.dirty = state.shotsDirty;" in saved
    # The ordinary save clears them through the same helper, so the three paths cannot drift.
    assert "markDocumentsSaved();" in app_js_block("async function saveProject")

    consequence = run_module("""
      import { UNSAVED_DOCUMENT_EDITS_CONSEQUENCE } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({ consequence: UNSAVED_DOCUMENT_EDITS_CONSEQUENCE }));
    """)["consequence"].lower()
    # It says *why* the edits cannot come back, not merely "are you sure".
    assert "only stored text" in consequence
    assert "unsaved edits cannot be restored" in consequence


def test_lock_toggle_confirms_the_lock_and_reverts_the_control_when_the_save_fails():
    """A generic "Project saved" says nothing about whether the document is now protected.

    And a failed save leaves the checkbox asserting a lock the server does not have, which is
    worse than not offering the control: the Director believes the document is protected while
    the next reply is free to replace it. So the control reverts to the stored state on failure
    -- the control only, since reverting the editors would discard unsaved typing.
    """
    script = """
      import { documentLockNotice } from './src/music_video_producer/web/assets/api.js';
      const attempt = (fn) => { try { return fn(); } catch (error) { return `THREW: ${error.message}`; } };
      console.log(JSON.stringify({
        treatmentLocked: documentLockNotice('treatment', true),
        treatmentUnlocked: documentLockNotice('treatment', false),
        styleLocked: documentLockNotice('style_bible', true),
        unknown: attempt(() => documentLockNotice('creative_brief', true)),
      }));
    """

    notices = run_module(script)
    assert notices["treatmentLocked"].startswith(f"{DOCUMENT_LABELS['treatment']} is locked")
    assert notices["treatmentUnlocked"].startswith(f"{DOCUMENT_LABELS['treatment']} is unlocked")
    assert notices["styleLocked"].startswith(f"{DOCUMENT_LABELS['style_bible']} is locked")
    assert "THREW: Unknown document" in notices["unknown"]

    binding = app_js_block('$(control.lock).addEventListener("change"', "    });")
    assert "documentLockNotice(documentKey, event.currentTarget.checked)" in binding
    assert "Project saved" not in binding
    assert "syncDocumentControls();" in binding
    # Reverting through renderTreatment would overwrite the textareas from the server, which
    # is the very discard this feature exists to stop.
    assert "renderTreatment" not in binding
    for editor in ("#treatment-text", "#style-bible", "#creative-brief"):
        assert editor not in app_js_block("function syncDocumentControls"), editor

    # The handler can only tell that the save failed because saveProject reports it.
    save = app_js_block("async function saveProject")
    assert "return true;" in save
    assert "return false;" in save
    # A click handler must not be handed the event as the toast wording.
    for selector in ('$("#save-project")', '$("#save-treatment")'):
        assert f'{selector}.addEventListener("click", () => saveProject());' in (
            APP_JS.read_text(encoding="utf-8")
        ), selector


def test_system_audit_line_is_visually_distinct_from_director_prose():
    """The restore line is the audit trail, rendered into the same thread as Director prose.

    Only `.message.user` and `.message.assistant` were styled, so the audit record was
    indistinguishable from something the Director said. Its colours come from the existing
    palette tokens -- an invented hex here would drift from every other surface.
    """
    css = STYLES_CSS.read_text(encoding="utf-8")

    rule = re.search(r"\.message\.system\s*\{([^}]*)\}", css)
    assert rule, "the restore audit line has no style of its own"
    body = rule.group(1)
    for role in (r"\.message\.user", r"\.message\.assistant"):
        assert re.search(role + r"\s*\{", css), role

    tokens = re.findall(r"var\((--[\w-]+)\)", body)
    assert tokens, body
    root = re.search(r":root\s*\{(.*?)\n\}", css, re.DOTALL)
    assert root, "styles.css no longer declares its palette on :root"
    for token in tokens:
        assert f"{token}:" in root.group(1), f"{token} is not a palette token"
    # No new colour: every colour in the rule is a token, not a literal.
    assert not re.search(r"#[0-9a-fA-F]{3,8}", body), body
    # And the role reaches the stylesheet at all, executed rather than grepped for: the bubble is
    # built by `threadHtml`, so what a browser would receive is what this reads the class off.
    stamped = run_module("""
      import { threadHtml } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({ thread: threadHtml([
        { id: 'msg_1', role: 'user', content: 'Ask.' },
        { id: 'msg_2', role: 'assistant', content: 'Answer.' },
        { id: 'msg_3', role: 'system', content: 'Treatment was restored.' },
      ]) }));
    """)["thread"]
    for role in ("user", "assistant", "system"):
        assert f'<div class="message {role}">' in stamped, role


def test_document_prose_names_come_only_from_the_label_mapping():
    """`DOCUMENT_LABELS` exists to centralise what each document is called on screen.

    The restore handler, the control render and the markup's own labels each spelled the names
    out again, so a rename could leave a button, a tooltip and a toast disagreeing about the
    same document -- and nothing covered any of them.
    """
    controls = document_controls()

    for label, excerpt in (
        ("restore handler", app_js_block("async function restoreDocument")),
        ("control sync", app_js_block("function syncDocumentControls")),
        ("lock binding", app_js_block('$(control.lock).addEventListener("change"', "    });")),
    ):
        code = without_comments(excerpt).lower()
        for prose in ("treatment", "style bible", "style-bible", "style_bible"):
            assert prose not in code, f"{label} names {prose!r} instead of using the mapping"
    assert "documentLabel(documentKey)" in APP_JS.read_text(encoding="utf-8")

    # The markup cannot import the mapping, so the test is what keeps its wording agreeing.
    for document, control in controls.items():
        text = re.sub(r"<[^>]+>", " ", scoped_control_group(control["tab"])).lower()
        label = DOCUMENT_LABELS[document].lower()
        assert f"lock {label}" in " ".join(text.split()), (document, text)
        assert f"restore {label}" in " ".join(text.split()), (document, text)


def test_validation_errors_render_readable_field_messages():
    script = """
      import { errorMessage } from './src/music_video_producer/web/assets/api.js';
      const response = { status: 422, statusText: 'Unprocessable Entity' };
      const validation = errorMessage({ detail: [
        { loc: ['body', 'lyrics'], msg: 'String should have at most 8000 characters', type: 'string_too_long' },
        { loc: ['body', 'duration'], msg: 'Input should be less than or equal to 200', type: 'less_than_equal' },
      ] }, response);
      const plain = errorMessage({ detail: 'ComfyUI is unreachable' }, { status: 502, statusText: 'Bad Gateway' });
      const empty = errorMessage(null, { status: 500, statusText: 'Internal Server Error' });
      console.log(JSON.stringify({ validation, plain, empty }));
    """

    messages = run_module(script)
    assert messages["validation"] == (
        "lyrics: String should have at most 8000 characters; "
        "duration: Input should be less than or equal to 200"
    )
    assert "[object Object]" not in messages["validation"]
    assert messages["plain"] == "ComfyUI is unreachable"
    assert messages["empty"] == "500 Internal Server Error"


def test_comfy_output_url_preserves_output_subfolder():
    script = """
      import { comfyOutputUrl } from './src/music_video_producer/web/assets/api.js';
      console.log(comfyOutputUrl('http://127.0.0.1:8188', 'music-video-producer/project/assets/image.png'));
    """

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == (
        "http://127.0.0.1:8188/view?"
        "filename=image.png&subfolder=music-video-producer%2Fproject%2Fassets&type=output"
    )


def test_remove_song_client_matches_a_route_the_server_actually_exposes():
    """The browser's DELETE path and query key, read off both sides and compared.

    `api.removeSong` hand-writes its URL, and the neighbouring upload route is plural
    (`/songs/upload`) while removal is singular (`/song`), so this is a live typo risk.
    Renaming the path or the flag leaves every other test green: the route tests write
    their own URLs and the handler test only greps for `api.removeSong(`. In a browser
    that mutation 404s on every project, or -- with only the flag renamed -- refuses
    forever on exactly the projects the feature was built for, since the parameter
    would silently default to False.
    """
    from music_video_producer.app import create_app

    source = API_JS.read_text(encoding="utf-8")
    call = source.split("removeSong:", 1)[1].split("\n", 1)[0]

    url = re.search(r"`([^`]+)`", call)
    assert url, "api.removeSong no longer builds its URL from a template literal"
    path, _, query = url.group(1).partition("?")

    assert "method: \"DELETE\"" in call, "song removal must be a DELETE"

    # `/api/projects/${id}/song` -> the FastAPI path shape `/api/projects/{project_id}/song`
    template = re.sub(r"\$\{[^}]+\}", "{project_id}", path)
    exposed = {route.path for route in create_app().routes}
    assert template in exposed, f"{template} is not a route the app exposes"

    # The query key must be the parameter name the route declares, not a near-miss.
    key = query.partition("=")[0]
    signature = inspect.signature(next(
        route.endpoint for route in create_app().routes
        if route.path == template and "DELETE" in getattr(route, "methods", set())
    ))
    assert key in signature.parameters, (
        f"api.removeSong sends `{key}`, which DELETE {template} does not accept"
    )


def test_song_refusal_is_recognised_and_recovered_from_rather_than_just_toasted():
    """A refusal means the server knows about shots this client does not.

    Without refreshing, every retry re-reads the same stale project, sends
    confirmed=false again, and fails identically -- the Director is stuck looking at
    the server's `confirm_song_replacement=true` instruction with no way to act on it.
    """
    script = """
      import { songRefusalMessage, SONG_REFUSAL_MARKER } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        marker: SONG_REFUSAL_MARKER,
        refusal: songRefusalMessage('shot windows are absolute seconds. Send confirm_song_replacement=true to proceed.'),
        other: songRefusalMessage('ComfyUI returned 400: prompt outputs failed validation'),
        missing: songRefusalMessage(undefined),
        nonString: songRefusalMessage(409),
      }));
    """

    result = run_module(script)
    assert result["refusal"] is True
    assert result["other"] is False
    assert result["missing"] is False
    assert result["nonString"] is False
    # Keyed on the server's own instruction sentence so the two cannot drift silently.
    assert result["marker"] in SONG_REPLACEMENT_CONSEQUENCE

    source = APP_JS.read_text(encoding="utf-8")
    recovery = source.split("async function recoverFromSongRefusal", 1)[1].split("\n}", 1)[0]
    assert "songRefusalMessage(error.message)" in recovery
    assert "api.project(" in recovery, "a refusal must refresh the project, not just toast"
    assert "renderAll();" in recovery

    # Both song-changing handlers route their failures through it.
    for anchor in ('$("#import-song")', '$("#remove-song")'):
        handler = source.split(anchor + '.addEventListener("click"', 1)[1].split("  });", 1)[0]
        assert "recoverFromSongRefusal(error)" in handler, anchor


def app_py_submitting_routes() -> dict[str, tuple[str, inspect.Signature]]:
    """Every route in `app.py` whose own body calls `comfy.submit`, by function name.

    Enumerated off the live app rather than grepped, so a route added, renamed or moved is
    classified by this test the moment it exists. Reading the source of each endpoint is what
    makes "and the guard sits *before* the submission" assertable at all.
    """
    from music_video_producer.app import create_app

    submitters: dict[str, tuple[str, inspect.Signature]] = {}
    for route in create_app().routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        try:
            source = inspect.getsource(endpoint)
        except (OSError, TypeError):  # pragma: no cover - a builtin or C-level endpoint
            continue
        if "comfy.submit" in source:
            submitters[endpoint.__name__] = (source, inspect.signature(endpoint))
    return submitters


def test_every_shot_sourced_submission_is_behind_the_readiness_gate():
    """No sibling path escapes the guard, now or when the next one is written.

    The gate is only worth what its least-guarded call site is worth: one route that builds a
    render payload from a Shot without asking readiness first spends a full GPU pass returning
    noise, and the pre-flight Epic 4 builds on this would be telling the Director the plan is
    fine while that path submits anyway. So the call sites are enumerated rather than listed,
    and every one that takes a `shot_id` has to be guarded.
    """
    from music_video_producer.batch import readiness_refusal

    submitters = app_py_submitting_routes()
    # Pinned, so a new submitting route cannot appear without this test being read. The
    # non-Shot routes render a Song, an image or an Asset and have no prompt of a Shot's to
    # check — `edit_asset` (AI Mod, 2026-08-19) is asset-sourced like multiview: its input
    # is an Asset's own image and instruction, gated by its own refusals, never a Shot.
    assert set(submitters) == {
        "generate_music",
        "generate_songplanner",
        "generate_flux",
        "generate_multiview",
        "edit_asset",
        "fill_assets",
        "generate_h3",
        "enhance_with_ltx25",
        "restore_song_audio",
    }, "a new route submits to ComfyUI; decide whether it is Shot-sourced and guard it"

    shot_sourced = {
        name: source
        for name, (source, signature) in submitters.items()
        if "shot_id" in signature.parameters
    }
    assert shot_sourced, "no route builds a payload from a Shot any more; this test is stale"

    # The two Shot-sourced routes the readiness gate does not apply to, and the decision is
    # recorded here rather than made by omission. The gate refuses a Shot whose prompt would
    # spend a GPU pass returning noise; both of these take a *take* as their input and neither
    # graph has a prompt the Shot supplies — `enhance_with_ltx25`'s is fixed **empty** by its
    # export, and `restore_song_audio`'s graph has no text input at all and no model either.
    # A Shot with no prompt is processed exactly as well as one with a prompt on both, so
    # borrowing the gate would refuse a real take for a field the work never reads.
    #
    # Exempt from that gate, not from *a* gate: each route's precondition is that a take exists,
    # and each is asserted below with the same "before the submission" ordering the others get,
    # against its own refusal constants. A route with neither would fail this test.
    ungated = {
        "enhance_with_ltx25": ("ENHANCE_NO_TAKE_REFUSAL", "ENHANCE_MISSING_TAKE_REFUSAL"),
        # Plus the three that make this route's window the render's own: a shot that never rode
        # the master has no window to take, a project with no song has nothing to take it from,
        # and a take with no recorded lead cannot be placed against the song at all. All three
        # refuse before the submission for the same reason the take checks do.
        "restore_song_audio": (
            "RESTORE_AUDIO_NO_TAKE_REFUSAL",
            "RESTORE_AUDIO_MISSING_TAKE_REFUSAL",
            "RESTORE_AUDIO_NOT_SONG_AUDIO_REFUSAL",
            "RESTORE_AUDIO_NO_SONG_REFUSAL",
            "RESTORE_AUDIO_NO_LEAD_REFUSAL",
        ),
    }
    for name, refusals in sorted(ungated.items()):
        assert name in shot_sourced, f"{name} no longer takes a shot_id; this exemption is stale"
        code = "\n".join(
            line
            for line in shot_sourced[name].splitlines()
            if not line.strip().startswith("#")
        )
        assert "readiness_report(" not in code, name
        # The take is the input, so its absence is what this route refuses, before submitting.
        assert "latest_output" in code, name
        assert code.index("latest_output") < code.index("comfy.submit"), name
        for refusal in refusals:
            assert refusal in code, (name, refusal)
            assert code.index(refusal) < code.index("comfy.submit"), (name, refusal)

    for name, source in {
        name: source for name, source in shot_sourced.items() if name not in ungated
    }.items():
        # Comments dropped first: the guard is preceded by a comment that quotes both the
        # submission and the interpolation it has to precede, and an ordering assertion that
        # matched prose rather than code would be measuring its own explanation.
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        assert "readiness_report(" in code, name
        guard = code.index("readiness_report(")
        # Before anything is sent. A guard below this line refuses only after the GPU pass.
        assert guard < code.index("comfy.submit"), name
        # And before the reference branch interpolates the prompt into a populated sentence,
        # which is what makes an empty prompt invisible to every check downstream of it.
        if 'f"Reference map:' in code:
            assert guard < code.index('f"Reference map:'), name
        # The refusal is the shared one, not a sentence written at the call site.
        assert "readiness_refusal(" in code, name

    # The interpolation those orderings are about really is still there, and really is in the H3
    # route: a rename would otherwise turn the branch above into a silent no-op.
    assert 'f"Reference map:' in shot_sourced["generate_h3"]
    # And the wording it raises is really the shared constant, not a look-alike.
    assert "Not submitted: no prompt on" in readiness_refusal(["shot_x"])


def test_every_submitting_route_saves_its_job_record_before_it_submits():
    """The Director's 2026-08-21 ruling, pinned at every call site including the next one.

    The defect: these routes submitted the graph and *then* saved the job record. Once
    `ProjectStore.save` gained its lost-update refusal, a save race therefore answered 409 for
    a graph already queued — the GPU rendered, the take landed on disk, and nothing recorded
    it. Reversed, the race refuses before a byte reaches ComfyUI, which is the cheap direction
    to fail. The stated cost is an orphan if the process dies in the window, which is why the
    record goes out carrying `PENDING_SUBMISSION_PROMPT_ID` rather than an empty id: the
    reconciler settles it, where an empty id would read as local ffmpeg work.

    Enumerated off the live app, exactly like the readiness gate above and for the same reason
    — a submitting route added tomorrow is held to this the moment it exists, rather than when
    somebody remembers to add it to a list.
    """
    for name, (source, _signature) in sorted(app_py_submitting_routes().items()):
        # Comments dropped first: every one of these routes explains the ordering in a comment
        # that quotes both `store.save` and `comfy.submit`, and an ordering assertion matching
        # prose rather than code would be measuring its own explanation.
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        submit = code.index("comfy.submit")
        assert "store.save(" in code, name
        assert code.index("store.save(") < submit, (
            f"{name} submits before it saves; a save race there costs GPU time"
        )
        # And what it saved is the record, in the state that survives the window.
        assert "PENDING_SUBMISSION_PROMPT_ID" in code, name
        assert code.index("PENDING_SUBMISSION_PROMPT_ID") < submit, name
        # The record is *constructed* on the near side too. Building it afterwards from
        # `submission.prompt_id` and saving a placeholder ahead of it would satisfy the two
        # assertions above while recording nothing the reconciler could use.
        assert code.index("RenderJob(") < submit, name
        # The two halves that keep the window honest: a failed submission settles its record,
        # and an accepted one adopts the real id — both strictly after the submission.
        assert "settle_unsubmitted_jobs(" in code, name
        assert code.index("settle_unsubmitted_jobs(") > submit, name
        assert "accept_submission(" in code, name
        assert code.index("accept_submission(") > submit, name


def test_the_readiness_refusal_is_one_wording_shared_by_the_server_and_the_browser():
    """One rule refused in two places must be one sentence.

    The route refuses a single Shot and the queue handler refuses a whole batch, and a Director
    who hits the gate from either side has to read the same instruction. Two hand-written
    wordings is how the browser starts describing a gate the server no longer has -- the same
    failure the expansion refusal and the song refusal are held together against.
    """
    from music_video_producer.batch import (
        PLAN_WITHOUT_SHOTS,
        READINESS_REFUSAL,
        REFUSAL_NAME_LIMIT,
        prompt_is_missing,
        readiness_refusal,
    )

    many_names = [f"SHOT {index:02d} (shot_{index})" for index in range(1, 9)]
    shared = run_module(f"""
      import {{ PLAN_WITHOUT_SHOTS, READINESS_REFUSAL, REFUSAL_NAME_LIMIT, blockedShotIds,
        blockedShotLabels, promptIsMissing, readinessRefusal }}
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({{
        template: READINESS_REFUSAL,
        limit: REFUSAL_NAME_LIMIT,
        planWithoutShots: PLAN_WITHOUT_SHOTS,
        one: readinessRefusal(['shot_a']),
        many: readinessRefusal(['shot_a', 'shot_b']),
        // Both designed-for edges of the refusal: no names at all, and more names than it lists.
        none: readinessRefusal([]),
        absent: readinessRefusal(),
        overLimit: readinessRefusal({json.dumps(many_names)}),
        missing: [
          promptIsMissing({{ prompt: '' }}),
          promptIsMissing({{ prompt: '   \\n\\t' }}),
          promptIsMissing({{ prompt: ' x ' }}),
          promptIsMissing({{}}),
          promptIsMissing(undefined),
        ],
        blocked: blockedShotIds({{ blocking: [
          {{ shot_ids: ['a'], labels: ['SHOT 01 (a)'], reason: 'x' }},
          {{ shot_ids: ['b', 'c'], labels: ['SHOT 02 (b)', 'SHOT 03 (c)'], reason: 'y' }},
        ] }}),
        labels: blockedShotLabels({{ blocking: [
          {{ shot_ids: ['a'], labels: ['SHOT 01 (a)'], reason: 'x' }},
          {{ shot_ids: ['b', 'c'], labels: ['SHOT 02 (b)', 'SHOT 03 (c)'], reason: 'y' }},
        ] }}),
        // A note that carries no labels still names something rather than `undefined`.
        unlabelled: blockedShotLabels({{ blocking: [{{ shot_ids: ['a'], reason: 'x' }}] }}),
        emptyPlan: blockedShotIds({{ blocking: [{{ shot_ids: [], reason: 'the plan is empty' }}] }}),
        noReport: blockedShotIds(undefined),
      }}));
    """)

    assert shared["template"] == READINESS_REFUSAL
    assert shared["limit"] == REFUSAL_NAME_LIMIT
    assert shared["planWithoutShots"] == PLAN_WITHOUT_SHOTS
    assert shared["one"] == readiness_refusal(["shot_a"])
    assert shared["many"] == readiness_refusal(["shot_a", "shot_b"])
    # No names is the empty plan, not "no prompt on ." -- the empty-plan note carries no ids, so
    # every id extractor hands this exactly that.
    assert shared["none"] == readiness_refusal([]) == PLAN_WITHOUT_SHOTS
    assert shared["absent"] == PLAN_WITHOUT_SHOTS
    # And a batch over more Shots than the sentence lists stops listing at the same point the
    # server does, or one twenty-Shot refusal reads differently on each side.
    assert shared["overLimit"] == readiness_refusal(many_names)
    assert f"and {len(many_names) - REFUSAL_NAME_LIMIT} more" in shared["overLimit"]
    assert many_names[REFUSAL_NAME_LIMIT] not in shared["overLimit"]

    # The report's own display names are used, never rebuilt: the server numbers by manifest
    # position while the notes are in song order, so a browser-side numbering would disagree.
    assert shared["labels"] == ["SHOT 01 (a)", "SHOT 02 (b)", "SHOT 03 (c)"]
    assert shared["unlabelled"] == ["a"]

    # The emptiness rule is mirrored too, whitespace and all: a clip that looked prompted while
    # the server called it blank would put the refusal *after* the click, which is the whole
    # thing this makes visible beforehand.
    server = [
        prompt_is_missing(Shot(start=0, duration=5, prompt="")),
        prompt_is_missing(Shot(start=0, duration=5, prompt="   \n\t")),
        prompt_is_missing(Shot(start=0, duration=5, prompt=" x ")),
    ]
    assert shared["missing"][:3] == server == [True, True, False]
    # A shot object with no prompt field at all, and no shot at all, are both "missing" rather
    # than exceptions: this runs inside the timeline redraw, which must never throw.
    assert shared["missing"][3:] == [True, True]

    # The empty-plan note names no Shot, so it contributes no id rather than a placeholder.
    assert shared["blocked"] == ["a", "b", "c"]
    assert shared["emptyPlan"] == []
    assert shared["noReport"] == []


def queue_handler_body() -> str:
    """The `#queue-ready` handler's code, comments dropped. No import can reach it."""
    source = APP_JS.read_text(encoding="utf-8")
    handler = source.split('$("#queue-ready").addEventListener("click"', 1)[1].split("\n  });", 1)[0]
    return without_comments(handler)


def test_the_generate_all_handler_confirms_then_posts_one_server_batch():
    """The client loop is gone: one confirm, one POST, the server's report relayed.

    Position still matters — the confirm precedes the request, and `confirm_gpu: true`
    exists only inside the request the confirm guards, so a handler edit that sends the
    flag unconditionally or before the dialog fails here. The plan is re-decided at the
    click from the same function that drew the button, so the count the Director confirms
    is the count the request means.
    """
    body = queue_handler_body()

    assert "generateAllPlan(state.project, readinessReport, replace)" in body
    confirm = body.index("window.confirm(plan.confirm)")
    post = body.index("api.generateBatch(")
    assert confirm < post
    assert "confirm_gpu: true" in body
    assert body.index("confirm_gpu: true") > confirm
    # No client loop remains anywhere: FR-4's skip-and-continue is the server's act.
    assert "for (const shot" not in body
    assert "api.generateH3(" not in body
    # The server's report reaches the Director through the one relay.
    assert "batchReportToast(report)" in body


def test_a_project_switch_during_the_batch_post_abandons_the_reload():
    """The selector stays live while the POST is in flight.

    The id is captured before the await and the submission goes to it, so a switch cannot
    redirect the batch; and the reload afterwards is guarded, so a batch that finished
    after a switch does not pull the Director back to the project they left.
    """
    body = queue_handler_body()

    assert "const projectId = state.project.id;" in body
    assert body.index("const projectId") < body.index("await api.generateBatch")
    assert "api.generateBatch(projectId," in body
    assert "api.generateBatch(state.project" not in body
    for reload in re.findall(r"[^\n]*await loadProject\(projectId\)[^\n]*", body):
        assert "state.project?.id === projectId" in reload, reload


def test_the_batch_report_toast_relays_every_skip_in_the_servers_words():
    """FR-4's report, rendered: what queued, what was skipped, each skip in the sentence
    the single-shot route refused it with — never a reworded copy, never a bare count."""
    states = run_module("""
      import { batchReportToast } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        clean: batchReportToast({ submitted: [{}, {}, {}], skipped: [] }),
        mixed: batchReportToast({ submitted: [{}],
          skipped: [{ label: 'SHOT 02 (shot_b)', reason: 'No prompt on it.' }] }),
        nothing: batchReportToast({ submitted: [], skipped: [
          { label: 'SHOT 01 (shot_a)', reason: 'locked.' }] }),
        empty: batchReportToast(undefined),
      }));
    """)
    assert states["clean"] == "3 shots queued as one batch"
    assert states["mixed"].startswith("1 shot queued as one batch — 1 skipped.")
    assert "SHOT 02 (shot_b): No prompt on it." in states["mixed"]
    assert states["nothing"].startswith("Nothing queued — 1 skipped.")
    assert states["empty"] == "Nothing queued"


def test_the_readiness_client_matches_a_route_the_server_actually_exposes():
    """`api.readiness` hand-writes its URL, so a rename leaves every other test green."""
    from music_video_producer.app import create_app

    call = API_JS.read_text(encoding="utf-8").split("readiness:", 1)[1].split("\n", 1)[0]
    url = re.search(r"`([^`]+)`", call)
    assert url, "api.readiness no longer builds its URL from a template literal"

    template = re.sub(r"\$\{[^}]+\}", "{project_id}", url.group(1))
    assert template in {route.path for route in create_app().routes}, template
    # A GET: readiness is derived and must never be a write.
    assert "method:" not in call, call


def test_an_unprompted_clip_says_so_rather_than_borrowing_the_untitled_fallback():
    """Emptiness has to be visible before the click, and not by colour alone.

    The clip rendered `shot.prompt || "Untitled shot"`, which is exactly what a real prompt
    reading "Untitled shot" renders -- so the one state that costs a wasted GPU pass looked
    identical to a named shot until the submission failed.

    Executed rather than grepped, because the substring assertions this replaces were all
    satisfied by the template with its ternary arms *swapped*: `NO PROMPT` stamped onto every
    written clip and the unprompted one rendered empty, with the whole suite green. The decision
    is a pure function now, and both states are asserted from its real output.
    """
    script = """
      import { SHOT_WITHOUT_PROMPT_FLAG, SHOT_WITH_PLACEHOLDER_FLAG, shotPromptCell }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        flag: SHOT_WITHOUT_PROMPT_FLAG,
        placeholderFlag: SHOT_WITH_PLACEHOLDER_FLAG,
        blank: shotPromptCell({ id: 'shot_a', prompt: '' }),
        whitespace: shotPromptCell({ id: 'shot_a', prompt: '  \\n\\t ' }),
        written: shotPromptCell({ id: 'shot_b', prompt: 'A singer turns toward camera' }),
        // The prompt every new shot is created with: the server blocks it, so the clip must too.
        placeholder: shotPromptCell({ id: 'shot_c', prompt: 'New shot' }),
        spacedPlaceholder: shotPromptCell({ id: 'shot_c', prompt: '  new   SHOT ' }),
        // ...but a real prompt that merely begins with those words is a real prompt.
        placeholderPrefix: shotPromptCell({ id: 'shot_d', prompt: 'New shot on the corridor' }),
        // A real prompt that happens to read like the flag is still a written shot.
        flagAsPrompt: shotPromptCell({ id: 'shot_e', prompt: 'NO PROMPT' }),
        untitled: shotPromptCell({ id: 'shot_f', prompt: 'Untitled shot' }),
        // The timeline redraws on every drag and keystroke, so neither of these may throw.
        missing: shotPromptCell({}),
        absent: shotPromptCell(),
      }));
    """

    cells = run_module(script)
    for empty in ("blank", "whitespace", "missing", "absent"):
        assert cells[empty]["blocked"] is True, empty
        assert cells[empty]["text"] == cells["flag"], empty
        assert cells[empty]["className"] == "no-prompt", empty
    # A written clip renders its prompt, carries no blocked class, and is not flagged.
    assert cells["written"] == {
        "blocked": False,
        "text": "A singer turns toward camera",
        "className": "",
        "label": "A singer turns toward camera",
    }
    for written in ("flagAsPrompt", "untitled", "placeholderPrefix"):
        assert cells[written]["blocked"] is False, written
        assert cells[written]["className"] == "", written
    # And the prompt text is what is drawn, not the fallback that made emptiness invisible.
    assert cells["untitled"]["text"] == "Untitled shot"
    assert cells["placeholderPrefix"]["text"] == "New shot on the corridor"

    # The placeholder blocks like a blank, but says which of the two it is: "no prompt" over a clip
    # whose prompt is visibly there sends the Director looking for something else.
    for placeholder in ("placeholder", "spacedPlaceholder"):
        assert cells[placeholder]["blocked"] is True, placeholder
        assert cells[placeholder]["className"] == "no-prompt", placeholder
        assert cells[placeholder]["text"] == cells["placeholderFlag"], placeholder
    assert cells["placeholderFlag"] != cells["flag"]


def test_the_client_and_server_agree_on_every_prompt_that_cannot_be_submitted():
    """Both implementations of the emptiness rule, executed over one matrix and compared.

    The rule grew a second case -- the `"New shot"` placeholder every added Shot is created with,
    compared after case and whitespace collapse -- and the old shared-wording test only ever
    exercised `""`, whitespace and one written prompt. A client that missed the placeholder draws a
    plan of default clips as fully prompted and is then refused by the route, which is worse than
    either half alone; one that over-matched would flag real prompts that merely start that way.

    The *reasons* are compared too, not just the booleans: they are what the clip's tooltip and the
    report's notes say, so a client sentence that drifted from the server's would leave the Director
    reading two accounts of one refusal.
    """
    from music_video_producer.batch import PLACEHOLDER_PROMPT, prompt_is_missing, prompt_rejection

    prompts = [
        "",
        "   \n\t ",
        PLACEHOLDER_PROMPT,
        "new shot",
        "  New   SHOT  ",
        "\tNew\nshot ",
        "New shot on the corridor",
        "New shots",
        "shot",
        "A singer turns toward camera",
        "NO PROMPT",
        "Untitled shot",
    ]
    script = f"""
      import {{ PLACEHOLDER_PROMPT, promptIsMissing, promptRejection }}
        from './src/music_video_producer/web/assets/api.js';
      const prompts = {json.dumps(prompts)};
      console.log(JSON.stringify({{
        placeholder: PLACEHOLDER_PROMPT,
        rejections: prompts.map((prompt) => promptRejection({{ prompt }})),
        missing: prompts.map((prompt) => promptIsMissing({{ prompt }})),
        // Neither may throw: this runs inside the timeline redraw.
        noShot: [promptIsMissing({{}}), promptIsMissing(undefined), promptIsMissing(null)],
      }}));
    """

    browser = run_module(script)
    assert browser["placeholder"] == PLACEHOLDER_PROMPT
    assert browser["rejections"] == [prompt_rejection(prompt) for prompt in prompts]
    assert browser["missing"] == [
        prompt_is_missing(Shot(start=0, duration=5, prompt=prompt)) for prompt in prompts
    ]
    # The matrix really does cover both refusals and a pass, or comparing two lists proves nothing.
    assert browser["missing"][:6] == [True] * 6
    assert browser["missing"][6:] == [False] * 6
    assert len(set(browser["rejections"][:6])) == 2, "the two refusals are not told apart"
    assert browser["noShot"] == [True, True, True]

    # And the string the rule is about is the string the app actually writes. A second spelling at
    # the creation site would produce Shots the timeline draws as prompted and the route refuses --
    # which is precisely how the placeholder walked through the gate in the first place.
    source = APP_JS.read_text(encoding="utf-8")
    add_shot = source.split('$("#add-shot").addEventListener("click"', 1)[1].split("  });", 1)[0]
    assert "prompt: PLACEHOLDER_PROMPT," in add_shot
    assert f'"{PLACEHOLDER_PROMPT}"' not in source, "the placeholder is spelled out instead of imported"


def test_a_blocked_clip_carries_accessible_text_saying_what_to_do_about_it():
    """A word and a dashed border are not a state a screen reader announces.

    The flag is the only text a blocked clip had, and it says *that* something is wrong without
    saying what or how to fix it -- while the refusal that follows the click states both. One
    remedy wording, on the clip before the click and in the refusal after it, and the reason itself
    is the server's own sentence rather than a second account of the same fact.
    """
    from music_video_producer.batch import (
        READINESS_REFUSAL,
        SHOT_WITH_PLACEHOLDER_PROMPT,
        SHOT_WITHOUT_PROMPT,
    )

    shared = run_module("""
      import { READINESS_REMEDY, shotPromptCell, shotPromptHelp }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        remedy: READINESS_REMEDY,
        blank: shotPromptCell({ id: 'shot_a', prompt: '   ' }).label,
        placeholder: shotPromptCell({ id: 'shot_b', prompt: 'New shot' }).label,
        written: shotPromptCell({ id: 'shot_c', prompt: 'A singer turns toward camera' }).label,
        writtenHelp: shotPromptHelp({ id: 'shot_c', prompt: 'A singer turns toward camera' }),
      }))
    """)

    # The instruction the Director reads before the click is the one the server's refusal gives.
    assert shared["remedy"] in READINESS_REFUSAL
    # And the diagnosis is the server's own note, per case, with that instruction appended.
    assert shared["blank"] == f"{SHOT_WITHOUT_PROMPT} {shared['remedy']}."
    assert shared["placeholder"] == f"{SHOT_WITH_PLACEHOLDER_PROMPT} {shared['remedy']}."
    # A written clip's accessible name is its own prompt, which the two-line clamp hides.
    assert shared["written"] == "A singer turns toward camera"
    assert shared["writtenHelp"] == ""


def test_the_timeline_clip_renders_only_what_the_prompt_cell_decided():
    """Source-level companion: the decision must not be re-made in the template.

    Nothing in the suite can execute `renderTimeline`, so this is what keeps the DOM layer a thin
    applier -- an emptiness test written here again is a second rule that can disagree with the
    one that is tested.
    """
    clip = APP_JS.read_text(encoding="utf-8").split("function renderTimeline", 1)[1].split("\n}", 1)[0]
    body = without_comments(clip)

    # The call now carries the shot's live render percentage, which `shotPromptCell` folds into
    # the label it returns. The rule this test exists for is unchanged and is asserted exactly:
    # the template makes no decision of its own, it applies one. Both arguments are looked up
    # above the call and neither is computed inside the template string.
    assert "const percent = state.renderProgress?.[shot.id];" in body
    assert "const cell = shotPromptCell(shot, percent);" in body
    for drawn in ("cell.className", "escapeHtml(cell.text)"):
        assert drawn in body, drawn
    # Both the tooltip and the accessible name, from the one label -- which is now
    # `clipWindowState`'s, because the shot-length band folds its own sentence into the same
    # string rather than the template joining two. `band.label` *is* `cell.label` for every clip
    # inside the band, and `clipWindowState` is executed for both arms by its own test.
    assert "const band = clipWindowState(windowKinds[shot.id], cell.label);" in body
    assert 'title="${escapeHtml(band.label)}"' in body
    assert 'aria-label="${escapeHtml(band.label)}"' in body
    # The render-state word is applied the same way: one function decides it, including whether
    # the percentage is known at all, and the template only chooses whether the span exists.
    assert '<span class="clip-state">${escapeHtml(renderingFlag(percent))}</span>' in body
    # No second copy of either decision, in any of its spellings. `RENDERING` spelled out here
    # would be a template that had stopped asking `renderingFlag` what the word is -- which is
    # also where a hand-formatted percentage would appear.
    for redecided in (
        "promptIsMissing",
        "SHOT_WITHOUT_PROMPT_FLAG",
        "Untitled shot",
        "RENDERING",
    ):
        assert redecided not in body, redecided


def test_the_render_again_control_is_decided_by_executing_it_for_every_state():
    """Every state the control can be in, run rather than read.

    Three outcomes have to be told apart and none of them is inferable from the others: not shown
    at all, shown but refused with the reason, and shown and ready. `disabled` is not the negation
    of `shown` -- an approved shot is shown *and* disabled, and that is the case carrying the
    sentence worth reading -- so a design that collapsed the two would fail here rather than
    quietly hiding the one control that had something to say.

    The prompt cases are the design note of the whole feature. A shot that rendered successfully
    and then had its prompt deleted must stop offering this control immediately, from the prompt
    in the textarea rather than from any memory of having passed the gate once.
    """
    from music_video_producer.app import RENDER_AGAIN_STATUSES

    states = run_module("""
      import { RENDER_AGAIN_APPROVED, RENDER_AGAIN_HELP, RENDER_AGAIN_LABEL, RENDER_AGAIN_LOCKED,
        RENDER_AGAIN_STATUSES, READINESS_REMEDY, renderAgainControl }
        from './src/music_video_producer/web/assets/api.js';
      const shot = (fields) => ({ id: 'shot_a', prompt: 'A singer turns toward camera', locked: false, approved_output: '', ...fields });
      const seen = {};
      for (const status of ['draft', 'ready', 'queued', 'running', 'complete', 'error', 'approved']) {
        seen[status] = renderAgainControl(shot({ status }));
      }
      console.log(JSON.stringify({
        statuses: RENDER_AGAIN_STATUSES,
        label: RENDER_AGAIN_LABEL,
        help: RENDER_AGAIN_HELP,
        lockedText: RENDER_AGAIN_LOCKED,
        approvedText: RENDER_AGAIN_APPROVED,
        remedy: READINESS_REMEDY,
        seen,
        locked: renderAgainControl(shot({ status: 'complete', locked: true })),
        approved: renderAgainControl(shot({ status: 'complete', approved_output: 'takes/one.mp4' })),
        // A locked *and* approved shot reads its lock first, because unlocking is what has to
        // happen before anything else can.
        lockedAndApproved: renderAgainControl(shot({ status: 'complete', locked: true, approved_output: 'takes/one.mp4' })),
        emptied: renderAgainControl(shot({ status: 'complete', prompt: '' })),
        whitespace: renderAgainControl(shot({ status: 'complete', prompt: '  \\n\\t ' })),
        placeholder: renderAgainControl(shot({ status: 'complete', prompt: 'New shot' })),
        errored: renderAgainControl(shot({ status: 'error' })),
        nothing: renderAgainControl(undefined),
      }));
    """)

    # The status list is the server's, so the control is never offered for a status the route
    # would not re-open.
    assert states["statuses"] == list(RENDER_AGAIN_STATUSES)

    # The one-click flow's seed stride is the server's own (2026-08-19, the Director's live
    # report: render-again re-opened the shot and nothing rendered — the click now re-opens,
    # strides the seed and queues, and a stride that drifted from the batch's would make the
    # lone click and the batch produce different takes from the same starting seed).
    from music_video_producer.app import RESUBMIT_SEED_STRIDE

    stride = run_module("""
      import { RESUBMIT_SEED_STRIDE } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({ stride: RESUBMIT_SEED_STRIDE }));
    """)
    assert stride["stride"] == RESUBMIT_SEED_STRIDE

    # Not applicable: nothing to render again, so no control and nothing to explain.
    for status in ("draft", "ready", "queued", "running"):
        assert states["seen"][status]["shown"] is False, status
        assert states["seen"][status]["title"] == "", status
    # Applicable, and for an errored shot too -- the likeliest use of the whole action.
    assert states["seen"]["complete"] == {
        "shown": True, "disabled": False, "label": states["label"],
        "title": states["help"], "reason": "",
    }
    assert states["errored"]["shown"] is True
    assert states["errored"]["disabled"] is False

    # Refused, shown, and carrying the reason -- which is the state a hide-it design loses.
    assert states["locked"]["shown"] is True
    assert states["locked"]["disabled"] is True
    assert states["locked"]["reason"] == states["lockedText"]
    assert states["approved"]["reason"] == states["approvedText"]
    assert states["lockedAndApproved"]["reason"] == states["lockedText"]
    # The `approved` status alone is refused as well, with no approved_output at all.
    assert states["seen"]["approved"]["shown"] is True
    assert states["seen"]["approved"]["disabled"] is True
    assert states["seen"]["approved"]["reason"] == states["approvedText"]

    # The gate, asked again on a shot that has already passed it once.
    for case in ("emptied", "whitespace", "placeholder"):
        assert states[case]["shown"] is True, case
        assert states[case]["disabled"] is True, case
        assert states[case]["reason"].endswith(f"{states['remedy']}."), case
    assert "no prompt" in states["emptied"]["reason"]
    assert "placeholder" in states["placeholder"]["reason"]

    # And nothing at all is not a shot with something to re-render.
    assert states["nothing"]["shown"] is False


def test_the_shot_inspector_draws_and_binds_the_render_again_control_it_was_given():
    """The control, rendered and clicked, against the workspace's own code.

    Two things this proves that no amount of source reading can. The inspector really applies what
    `renderAgainControl` decided -- so a template that drew the button for every shot, or dropped
    the `disabled`, would fail here rather than pass on the strength of the decision function being
    correct and unused. And the click really reaches the purpose-built route: `PUT /shots` is the
    generic full-project write that had to be used by hand to do this, and a control wired to it
    would look identical in the source and would carry the whole shot list on the wire.
    """
    rendered = run_workspace("""
      const project = (fields) => ({
        id: 'p1', assets: [], jobs: [], song: null,
        shots: [{ id: 'shot_a', start: 0, duration: 5, prompt: 'A singer turns toward camera',
                  mode: 'text', asset_ids: [], reference_labels: {}, use_song_audio: false,
                  seed: 0, status: 'complete', prompt_id: 'p-1', latest_output: 'takes/one.mp4',
                  approved_output: '', locked: false, ...fields }],
      });
      const draw = (fields) => {
        state.project = project(fields);
        state.selectedShotId = 'shot_a';
        app.renderShotInspector();
        const html = at('#shot-inspector').innerHTML;
        return {
          present: html.includes('id="render-again"'),
          disabled: /id="render-again"[^>]*\\sdisabled/.test(html),
          html,
        };
      };
      const complete = draw({});
      const locked = draw({ locked: true });
      const approved = draw({ approved_output: 'takes/one.mp4' });
      const emptied = draw({ prompt: '' });
      const draft = draw({ status: 'draft', prompt: 'New shot', latest_output: '' });
      const running = draw({ status: 'running' });

      // Back to the shot that may be re-opened, and click it. The reply is a project whose shot
      // is now ready, exactly as the route returns.
      draw({});
      const reopened = project({ status: 'ready' });
      globalThis.fetch = (path, options = {}) => {
        requests.push({ path, method: options.method || 'GET', body: options.body || null });
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: { get: () => 'application/json' },
          json: async () => reopened,
        });
      };
      const toasts = [];
      globalThis.document.createElement = () => { const item = make('<toast>'); toasts.push(item); return item; };
      // The boot requests this module fires on import are not this click's; only what the click
      // itself puts on the wire is the thing under test.
      //
      // First arm: the Director cancels the queue question. The old contract exactly — one
      // bodyless request to the purpose-built route, the reply adopted, nothing rendered.
      answer(false);
      await fire('#render-again:click', {});
      const cancelled = { requests: [...requests], status: state.project.shots[0].status,
                          toasts: toasts.map((item) => item.textContent) };
      // Second arm: the Director accepts. Re-open, stride the seed through the shots write,
      // queue one turbo take — the 2026-08-19 live report was this click re-opening the shot
      // and stopping, which read as "nothing came across ComfyUI".
      draw({});
      toasts.length = 0;
      answer(true);
      await fire('#render-again:click', {});
      const queued = { requests: [...requests],
                       toasts: toasts.map((item) => item.textContent) };
      console.log(JSON.stringify({
        complete: { present: complete.present, disabled: complete.disabled },
        locked: { present: locked.present, disabled: locked.disabled, reason: locked.html.includes(contract.RENDER_AGAIN_LOCKED) },
        approved: { present: approved.present, disabled: approved.disabled, reason: approved.html.includes(contract.RENDER_AGAIN_APPROVED) },
        emptied: { present: emptied.present, disabled: emptied.disabled },
        draft: { present: draft.present },
        running: { present: running.present },
        cancelled,
        queued,
        notice: contract.renderAgainNotice(reopened, 'shot_a'),
        stride: contract.RESUBMIT_SEED_STRIDE,
      }));
    """)

    # Drawn only where it applies, and disabled where it is refused.
    assert rendered["complete"] == {"present": True, "disabled": False}
    assert rendered["locked"] == {"present": True, "disabled": True, "reason": True}
    assert rendered["approved"] == {"present": True, "disabled": True, "reason": True}
    assert rendered["emptied"] == {"present": True, "disabled": True}
    assert rendered["draft"]["present"] is False
    assert rendered["running"]["present"] is False

    # Cancelled: one request, to the purpose-built route, with no body — nothing a stale
    # client could reassert over the rest of the plan travelled with it, and no GPU spent.
    assert rendered["cancelled"]["requests"] == [
        {"path": "/api/projects/p1/shots/shot_a/render-again", "method": "POST", "body": None}
    ]
    # The reply is adopted, so the panel and the batch button redraw from it.
    assert rendered["cancelled"]["status"] == "ready"
    # And the Director is told what happened to the take that is already there, rather than
    # being left to assume the application is keeping both.
    assert rendered["cancelled"]["toasts"] == [rendered["notice"]]
    assert "SHOT 01 (shot_a)" in rendered["notice"]

    # Accepted: re-open, stride the seed, queue one take — in that order, because the render
    # reads the seed from the store and a stride that landed after submission would render
    # the identical take (the "nothing was replaced" the one-gesture flow exists to end).
    # Reads (the readiness refresh the silent saver triggers, the reload) may interleave;
    # the writes and their order are the contract.
    writes = [
        sent for sent in rendered["queued"]["requests"] if sent["method"] in ("POST", "PUT")
    ]
    assert [sent["path"] for sent in writes][:3] == [
        "/api/projects/p1/shots/shot_a/render-again",
        "/api/projects/p1/shots",
        "/api/projects/p1/shots/shot_a/generate/h3",
    ]
    import json as json_module

    saved = json_module.loads(writes[1]["body"])
    assert saved["shots"][0]["seed"] == rendered["stride"]  # 0 + the server's own stride
    generate = json_module.loads(writes[2]["body"])
    assert generate["profile"] == "turbo"
    assert rendered["queued"]["toasts"] == [
        f"{rendered['notice']} A new take is rendering now."
    ]


def test_render_again_wordings_are_the_servers_own():
    """One rule, one sentence, whichever side the Director meets it on.

    The lock and the approval are refused by the route and previewed by the panel, and the
    previous-take statement is the server's account of what re-opening does. Two hand-written
    wordings for one rule is how the browser starts describing behaviour the server no longer has.
    """
    from music_video_producer.app import (
        RENDER_AGAIN_APPROVED_REFUSAL,
        RENDER_AGAIN_LOCKED_REFUSAL,
        RENDER_AGAIN_PREVIOUS_TAKE,
    )

    shared = run_module("""
      import { RENDER_AGAIN_APPROVED, RENDER_AGAIN_LOCKED, RENDER_AGAIN_PREVIOUS_TAKE,
        renderAgainNotice, shotLabel } from './src/music_video_producer/web/assets/api.js';
      const project = { shots: [{ id: 'shot_a' }, { id: 'shot_b' }, { id: 'shot_c' }] };
      console.log(JSON.stringify({
        locked: RENDER_AGAIN_LOCKED,
        approved: RENDER_AGAIN_APPROVED,
        previousTake: RENDER_AGAIN_PREVIOUS_TAKE,
        notice: renderAgainNotice(project, 'shot_c'),
        first: shotLabel(project, 'shot_a'),
        // A shot this client does not have is named by its bare id rather than by a position
        // it does not hold -- exactly what batch.shot_label does.
        absent: shotLabel(project, 'shot_z'),
      }));
    """)

    # The refusals are the server's sentence exactly, with "This shot" standing in for the label
    # the server prefixes: the panel is already showing the shot, so naming it there would name it
    # twice, and every other word has to be the same or the two sides describe different rules.
    assert shared["locked"] == RENDER_AGAIN_LOCKED_REFUSAL.format(shot="This shot")
    assert shared["approved"] == RENDER_AGAIN_APPROVED_REFUSAL.format(shot="This shot")
    assert shared["previousTake"] == RENDER_AGAIN_PREVIOUS_TAKE
    assert shared["notice"] == RENDER_AGAIN_PREVIOUS_TAKE.format(shot="SHOT 03 (shot_c)")
    assert shared["first"] == "SHOT 01 (shot_a)"
    assert shared["absent"] == "shot_z"


def test_the_mark_ready_control_is_decided_by_executing_it_for_every_state():
    """Every state the commit control can be in, run rather than read.

    Four outcomes have to be told apart and none is inferable from the others: not shown at all,
    shown pointing one way, shown pointing the other, and shown but refused with the reason.
    `disabled` is not the negation of `shown` -- a locked shot is shown *and* disabled, and that is
    the case carrying the sentence worth reading.

    `action` is asserted alongside the label because they are two halves of one claim: a button
    reading "Back to draft" wired to the arming route would look right in the panel and would arm
    the shot, and nothing about its appearance would say so.

    The prompt cases are the design note of the whole feature, and the asymmetry is deliberate. The
    gate runs in the arming direction only: `draft` is the un-armed state, so a control that
    refused to un-commit an unprompted shot would trap it armed.
    """
    from music_video_producer.app import MARK_READY_STATUSES

    states = run_module("""
      import { MARK_DRAFT_HELP, MARK_DRAFT_LABEL, MARK_READY_APPROVED, MARK_READY_HELP,
        MARK_READY_LABEL, MARK_READY_LOCKED, MARK_READY_STATUSES, READINESS_REMEDY,
        markReadyControl } from './src/music_video_producer/web/assets/api.js';
      const shot = (fields) => ({ id: 'shot_a', prompt: 'A singer turns toward camera', locked: false, approved_output: '', ...fields });
      const seen = {};
      for (const status of ['draft', 'ready', 'queued', 'running', 'complete', 'error', 'approved']) {
        seen[status] = markReadyControl(shot({ status }));
      }
      console.log(JSON.stringify({
        statuses: MARK_READY_STATUSES,
        readyLabel: MARK_READY_LABEL,
        draftLabel: MARK_DRAFT_LABEL,
        readyHelp: MARK_READY_HELP,
        draftHelp: MARK_DRAFT_HELP,
        lockedText: MARK_READY_LOCKED,
        approvedText: MARK_READY_APPROVED,
        remedy: READINESS_REMEDY,
        seen,
        locked: markReadyControl(shot({ status: 'draft', locked: true })),
        lockedReady: markReadyControl(shot({ status: 'ready', locked: true })),
        approved: markReadyControl(shot({ status: 'draft', approved_output: 'takes/one.mp4' })),
        // A locked *and* approved shot reads its lock first, because unlocking is what has to
        // happen before anything else can -- the server's guard order, mirrored.
        lockedAndApproved: markReadyControl(shot({ status: 'draft', locked: true, approved_output: 'takes/one.mp4' })),
        blank: markReadyControl(shot({ status: 'draft', prompt: '' })),
        whitespace: markReadyControl(shot({ status: 'draft', prompt: '  \\n\\t ' })),
        placeholder: markReadyControl(shot({ status: 'draft', prompt: 'New shot' })),
        // The other direction with the same empty prompt, which must NOT be refused.
        emptiedAndArmed: markReadyControl(shot({ status: 'ready', prompt: '' })),
        nothing: markReadyControl(undefined),
      }));
    """)

    # The status list is the server's, so the control is never offered for a status the routes do
    # not own.
    assert states["statuses"] == list(MARK_READY_STATUSES)

    # Not applicable: past the first render, and the render-again control's business instead.
    for status in ("queued", "running", "complete", "error", "approved"):
        assert states["seen"][status]["shown"] is False, status
        assert states["seen"][status]["title"] == "", status
        assert states["seen"][status]["action"] == "", status
    # Applicable, pointing the way the shot's own status decides.
    assert states["seen"]["draft"] == {
        "shown": True, "disabled": False, "action": "ready",
        "label": states["readyLabel"], "title": states["readyHelp"], "reason": "",
    }
    assert states["seen"]["ready"] == {
        "shown": True, "disabled": False, "action": "draft",
        "label": states["draftLabel"], "title": states["draftHelp"], "reason": "",
    }

    # Refused, shown, and carrying the reason -- which is the state a hide-it design loses. The
    # label still says which way the button would have gone, so the panel does not silently
    # relabel the action while explaining why it is off.
    assert states["locked"]["shown"] is True
    assert states["locked"]["disabled"] is True
    assert states["locked"]["reason"] == states["lockedText"]
    assert states["locked"]["label"] == states["readyLabel"]
    assert states["lockedReady"]["label"] == states["draftLabel"]
    assert states["lockedReady"]["reason"] == states["lockedText"]
    assert states["approved"]["reason"] == states["approvedText"]
    assert states["lockedAndApproved"]["reason"] == states["lockedText"]

    # The gate, in the arming direction, from the prompt on screen.
    for case in ("blank", "whitespace", "placeholder"):
        assert states[case]["shown"] is True, case
        assert states[case]["disabled"] is True, case
        assert states[case]["action"] == "ready", case
        assert states[case]["reason"].endswith(f"{states['remedy']}."), case
    assert "no prompt" in states["blank"]["reason"]
    assert "placeholder" in states["placeholder"]["reason"]
    # ...and emphatically not in the other one. An armed shot whose prompt was emptied is a shot
    # whose Director must be able to disarm it.
    assert states["emptiedAndArmed"]["disabled"] is False
    assert states["emptiedAndArmed"]["action"] == "draft"

    # And nothing at all is not a shot with a commitment to make.
    assert states["nothing"]["shown"] is False


def test_the_two_first_render_controls_partition_the_status_vocabulary():
    """Exactly one control is offered for any shot, and never neither.

    The two lists are complements by construction on the server, and the browser holds its own copy
    of each. If they drifted apart the visible failure is a status showing no control at all -- a
    shot the Director can look at and not move, reachable only by an API client, which is precisely
    the hole this story closed.
    """
    shown = run_module("""
      import { MARK_READY_STATUSES, RENDER_AGAIN_STATUSES, markReadyControl, renderAgainControl }
        from './src/music_video_producer/web/assets/api.js';
      const drawn = {};
      for (const status of ['draft', 'ready', 'queued', 'running', 'complete', 'error', 'approved']) {
        const shot = { id: 'shot_a', prompt: 'A singer turns', locked: false, approved_output: '', status };
        drawn[status] = { mark: markReadyControl(shot).shown, again: renderAgainControl(shot).shown };
      }
      console.log(JSON.stringify({ drawn, mark: MARK_READY_STATUSES, again: RENDER_AGAIN_STATUSES }));
    """)

    assert set(shown["mark"]).isdisjoint(shown["again"])
    for status, drawn in shown["drawn"].items():
        # `queued` and `running` are the one deliberate gap: a live render is nobody's to move, and
        # both controls say so by not being there.
        expected = 0 if status in ("queued", "running") else 1
        assert [drawn["mark"], drawn["again"]].count(True) == expected, status


def test_the_shot_inspector_draws_and_binds_the_mark_ready_control_it_was_given():
    """The control, rendered and clicked, against the workspace's own code.

    Two things this proves that no amount of source reading can. The inspector really applies what
    `markReadyControl` decided -- so a template that drew the button for every shot, or dropped the
    `disabled`, would fail here rather than pass on the strength of the decision function being
    correct and unused. And the click really reaches the purpose-built route in the direction the
    button claimed: `PUT /shots` is the generic full-project write that was the *only* way to set
    this field before, and a control wired to it would look identical in the source while carrying
    the whole plan on the wire.
    """
    rendered = run_workspace("""
      const project = (fields) => ({
        id: 'p1', assets: [], jobs: [], song: null,
        shots: [{ id: 'shot_a', start: 0, duration: 5, prompt: 'A singer turns toward camera',
                  mode: 'text', asset_ids: [], reference_labels: {}, use_song_audio: false,
                  seed: 0, status: 'draft', prompt_id: '', latest_output: '',
                  approved_output: '', locked: false, ...fields }],
      });
      const draw = (fields) => {
        state.project = project(fields);
        state.selectedShotId = 'shot_a';
        app.renderShotInspector();
        const html = at('#shot-inspector').innerHTML;
        return {
          present: html.includes('id="mark-ready"'),
          disabled: /id="mark-ready"[^>]*\\sdisabled/.test(html),
          html,
        };
      };
      const drafted = draw({});
      const armed = draw({ status: 'ready' });
      const locked = draw({ locked: true });
      const approved = draw({ approved_output: 'takes/one.mp4' });
      const placeholder = draw({ prompt: 'New shot' });
      const rendered = draw({ status: 'complete', latest_output: 'takes/one.mp4' });

      // The click, in each direction, against a server that answers with the moved project.
      const clicked = async (fields, reply) => {
        draw(fields);
        globalThis.fetch = (path, options = {}) => {
          requests.push({ path, method: options.method || 'GET', body: options.body || null });
          return Promise.resolve({
            ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => reply,
          });
        };
        requests.length = 0;
        toasts.length = 0;
        await fire('#mark-ready:click', {});
        return { requests: [...requests], status: state.project.shots[0].status, toasts: [...toasts] };
      };
      const toasts = [];
      globalThis.document.createElement = () => { const item = make('<toast>'); toasts.push(item); return item; };
      const arming = await clicked({}, project({ status: 'ready' }));
      const disarming = await clicked({ status: 'ready' }, project({ status: 'draft' }));

      console.log(JSON.stringify({
        drafted: { present: drafted.present, disabled: drafted.disabled, label: drafted.html.includes(contract.MARK_READY_LABEL) },
        armed: { present: armed.present, disabled: armed.disabled, label: armed.html.includes(contract.MARK_DRAFT_LABEL) },
        locked: { present: locked.present, disabled: locked.disabled, reason: locked.html.includes(contract.MARK_READY_LOCKED) },
        approved: { present: approved.present, disabled: approved.disabled, reason: approved.html.includes(contract.MARK_READY_APPROVED) },
        placeholder: { present: placeholder.present, disabled: placeholder.disabled },
        rendered: { present: rendered.present, again: rendered.html.includes('id="render-again"') },
        arming: { ...arming, toasts: arming.toasts.map((item) => item.textContent) },
        disarming: { ...disarming, toasts: disarming.toasts.map((item) => item.textContent) },
        readyNotice: contract.markReadyNotice(project({}), 'shot_a', 'ready'),
        draftNotice: contract.markReadyNotice(project({}), 'shot_a', 'draft'),
      }));
    """)

    # Drawn where it applies, pointing the right way, and disabled where it is refused.
    assert rendered["drafted"] == {"present": True, "disabled": False, "label": True}
    assert rendered["armed"] == {"present": True, "disabled": False, "label": True}
    assert rendered["locked"] == {"present": True, "disabled": True, "reason": True}
    assert rendered["approved"] == {"present": True, "disabled": True, "reason": True}
    assert rendered["placeholder"] == {"present": True, "disabled": True}
    # A shot past its first render gets the other control instead, and never both.
    assert rendered["rendered"] == {"present": False, "again": True}

    # One request per click, to the purpose-built route for the direction the button claimed, with
    # no body: nothing a stale client could reassert over the rest of the plan travelled with it.
    assert rendered["arming"]["requests"] == [
        {"path": "/api/projects/p1/shots/shot_a/mark-ready", "method": "POST", "body": None}
    ]
    assert rendered["disarming"]["requests"] == [
        {"path": "/api/projects/p1/shots/shot_a/mark-draft", "method": "POST", "body": None}
    ]
    # ...and emphatically not the generic shots write, which is the only thing that could do this
    # before and the reason it could not be done safely.
    for direction in ("arming", "disarming"):
        assert not any(
            sent["path"].endswith("/shots") for sent in rendered[direction]["requests"]
        ), direction

    # The reply is adopted, so the status chip, the timeline and the queue button redraw from it.
    assert rendered["arming"]["status"] == "ready"
    assert rendered["disarming"]["status"] == "draft"
    # And each direction says what did not happen, rather than leaving the Director to guess
    # whether a render just started.
    assert rendered["arming"]["toasts"] == [rendered["readyNotice"]]
    assert rendered["disarming"]["toasts"] == [rendered["draftNotice"]]
    assert "no GPU time" in rendered["readyNotice"]
    assert "nothing was deleted" in rendered["draftNotice"]
    assert "SHOT 01 (shot_a)" in rendered["readyNotice"]


def test_the_approval_control_is_decided_by_executing_it_for_every_state():
    """Every state the approve/un-approve pair can be in, run rather than read.

    Four outcomes have to be told apart and none is inferable from the others: nothing to decide
    about (no take, no control), approve, un-approve, and shown-but-refused with the in-flight
    reason. The approved arm reads *either* approval signal — the server's `shot_is_approved`
    definition — and wins over everything, because un-approve is the one way back and must be
    offered even on a Shot whose other fields have been hand-mangled.
    """
    states = run_module("""
      import { APPROVE_HELP, APPROVE_IN_FLIGHT, APPROVE_LABEL, UNAPPROVE_HELP, UNAPPROVE_LABEL,
        approvalControl } from './src/music_video_producer/web/assets/api.js';
      const shot = (fields) => ({ id: 'shot_a', prompt: 'A singer turns toward camera', locked: false,
        latest_output: '', approved_output: '', ...fields });
      const bare = {};
      const taken = {};
      for (const status of ['draft', 'ready', 'queued', 'running', 'complete', 'error', 'approved']) {
        bare[status] = approvalControl(shot({ status }));
        taken[status] = approvalControl(shot({ status, latest_output: 'takes/one.mp4' }));
      }
      console.log(JSON.stringify({
        approveLabel: APPROVE_LABEL, approveHelp: APPROVE_HELP, inFlight: APPROVE_IN_FLIGHT,
        unapproveLabel: UNAPPROVE_LABEL, unapproveHelp: UNAPPROVE_HELP,
        bare, taken,
        approved: approvalControl(shot({ status: 'approved', latest_output: 'takes/one.mp4', approved_output: 'takes/one.mp4' })),
        fieldOnly: approvalControl(shot({ status: 'complete', latest_output: 'takes/one.mp4', approved_output: 'takes/one.mp4' })),
        // A hand-mangled approval with no take at all still offers the one way back.
        mangled: approvalControl(shot({ status: 'draft', approved_output: 'takes/one.mp4' })),
        lockedApproved: approvalControl(shot({ status: 'approved', locked: true, approved_output: 'takes/one.mp4' })),
        nothing: approvalControl(undefined),
      }));
    """)

    # No take, no decision to make — for every status, including the in-flight pair. The one
    # exception is the `approved` status itself, whose bare form still offers the way back.
    for status in ("draft", "ready", "queued", "running", "complete", "error"):
        assert states["bare"][status]["shown"] is False, status
        assert states["bare"][status]["title"] == "", status
    assert states["bare"]["approved"]["action"] == "unapprove"

    # A settled take offers the approval, an errored shot's previous take included.
    for status in ("draft", "ready", "complete", "error"):
        assert states["taken"][status] == {
            "shown": True, "disabled": False, "action": "approve",
            "label": states["approveLabel"], "title": states["approveHelp"], "reason": "",
        }, status
    # In flight: shown and refused with the reason — the take on screen is about to be displaced.
    for status in ("queued", "running"):
        assert states["taken"][status]["shown"] is True, status
        assert states["taken"][status]["disabled"] is True, status
        assert states["taken"][status]["reason"] == states["inFlight"], status

    # Approved, by both signals or by either alone, reads as the un-approve direction — and a
    # lock does not take the way back away: approval is the Director's own decision to reverse.
    for case in ("approved", "fieldOnly", "mangled", "lockedApproved"):
        assert states[case] == {
            "shown": True, "disabled": False, "action": "unapprove",
            "label": states["unapproveLabel"], "title": states["unapproveHelp"], "reason": "",
        }, case
    assert states["taken"]["approved"]["action"] == "unapprove"

    assert states["nothing"]["shown"] is False


def test_the_assembly_control_is_decided_by_executing_it_for_every_state():
    """Every cheap readiness state the assembly bar can be in, run rather than read — plus the
    export reader's one job: the newest complete *local* job, told apart from ComfyUI post jobs
    by the empty prompt_id, exactly AD-9's marker.

    Deliberately only the cheap facts live in the client: gaps, overlaps and stale windows are
    the server's comprehensive 422, rendered verbatim. A second client-side implementation of
    the tiling rules is a second place for them to be wrong.
    """
    states = run_module("""
      import { ASSEMBLE_HELP, ASSEMBLE_LABEL, ASSEMBLE_NO_SHOTS, ASSEMBLE_NO_SONG,
        ASSEMBLE_RENDERS_OPEN, assemblyControl, latestAssemblyExport }
        from './src/music_video_producer/web/assets/api.js';
      const song = { path: 'media/song.wav' };
      const approved = (id) => ({ id, approved_output: `shots/${id}.mp4` });
      console.log(JSON.stringify({
        label: ASSEMBLE_LABEL, help: ASSEMBLE_HELP, noShots: ASSEMBLE_NO_SHOTS,
        noSong: ASSEMBLE_NO_SONG, rendersOpen: ASSEMBLE_RENDERS_OPEN,
        empty: assemblyControl({ shots: [], song }),
        songless: assemblyControl({ shots: [approved('a')], song: null }),
        unapproved: assemblyControl({ shots: [approved('a'), { id: 'b' }, { id: 'c' }], song }),
        open: assemblyControl({ shots: [approved('a')], song,
          jobs: [{ prompt_id: 'p-1', status: 'running' }] }),
        localOpen: assemblyControl({ shots: [approved('a')], song,
          jobs: [{ kind: 'post', prompt_id: '', status: 'running' }] }),
        ready: assemblyControl({ shots: [approved('a')], song,
          jobs: [{ prompt_id: 'p-1', status: 'complete' }] }),
        exportNone: latestAssemblyExport({ id: 'proj', jobs: [] }),
        exportNewest: latestAssemblyExport({ id: 'proj', jobs: [
          { id: 'job_old', kind: 'post', prompt_id: '', status: 'complete',
            output_files: ['exports/assembly_00001.mp4'] },
          { id: 'job_new', kind: 'post', prompt_id: '', status: 'complete',
            output_files: ['exports/assembly_00002.mp4'] },
          { id: 'job_comfy', kind: 'post', prompt_id: 'p-2', status: 'complete',
            output_files: ['shots/restored.mp4'] },
          { id: 'job_failed', kind: 'post', prompt_id: '', status: 'error', output_files: [] },
        ] }),
      }));
    """)

    for case, reason in (("empty", "noShots"), ("songless", "noSong"), ("open", "rendersOpen")):
        assert states[case]["disabled"] is True, case
        assert states[case]["reason"] == states[reason], case
    # The count is real: two of three shots lack an approval.
    assert states["unapproved"]["disabled"] is True
    assert "2 of 3" in states["unapproved"]["reason"]
    # A *local* open job does not read as "renders open" — the server's own 409 owns that
    # conflict, in its own words, and `hasActiveRenderJobs` rightly ignores empty prompt_ids.
    assert states["localOpen"]["disabled"] is False
    assert states["ready"] == {
        "disabled": False, "label": states["label"], "title": states["help"], "reason": "",
    }

    assert states["exportNone"] is None
    # Newest local export wins; the ComfyUI post job (a restore) and the failed run are not
    # exports and must not shadow it, whatever order they landed in.
    assert states["exportNewest"] == {
        "path": "exports/assembly_00002.mp4",
        "url": "/api/projects/proj/media/exports/assembly_00002.mp4",
        "jobId": "job_new",
    }


def test_the_assembly_client_calls_a_route_the_server_exposes_and_the_bar_is_wired():
    """The `removeSong` lesson applied to assembly: the hand-written URL is compared to the
    server's route table, and the bar's render is reachable from the timeline render every
    project load already goes through."""
    source = API_JS.read_text(encoding="utf-8")
    call = source.split("assemble:", 1)[1].split("\n", 1)[0]
    url = re.search(r"`([^`]+)`", call)
    assert url, "api.assemble no longer builds its URL from a template literal"
    client_path = url.group(1).replace("${id}", "{project_id}")
    assert client_path in {route.path for route in create_app().routes}

    workspace = APP_JS.read_text(encoding="utf-8")
    assert "renderAssembly();" in workspace.split("function renderTimeline()", 1)[1].split("\nfunction ", 1)[0], (
        "renderAssembly is no longer reached from renderTimeline — the bar would draw once and go stale"
    )
    assert 'id="assembly-bar"' in INDEX_HTML.read_text(encoding="utf-8")


def test_ai_mod_is_offered_to_image_assets_and_calls_a_real_route():
    """`aiModPlan` executed for every asset shape, and the `removeSong` lesson applied to
    the new call: the hand-written URL is compared against the server's route table."""
    states = run_module("""
      import { aiModPlan } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        character: aiModPlan({ kind: 'character', path: 'media/a.png' }),
        setting: aiModPlan({ kind: 'setting', path: 'media/b.png' }),
        editedChild: aiModPlan({ kind: 'character', path: 'assets/child.png' }),
        pending: aiModPlan({ kind: 'character', path: '' }),
        audio: aiModPlan({ kind: 'audio', path: 'media/song.mp3' }),
        video: aiModPlan({ kind: 'video', path: 'media/clip.mp4' }),
        nothing: aiModPlan(undefined),
      }));
    """)
    for ready in ("character", "setting", "editedChild"):
        assert states[ready] == {"ready": True}, ready
    assert states["pending"] == {"ready": False}
    for refused in ("audio", "video", "nothing"):
        assert states[refused] is None, refused

    source = API_JS.read_text(encoding="utf-8")
    call = source.split("editAsset:", 1)[1].split("\n", 1)[0]
    url = re.search(r"`([^`]+)`", call)
    assert url, "api.editAsset no longer builds its URL from a template literal"
    template = re.sub(r"\$\{projectId\}", "{project_id}", url.group(1))
    template = re.sub(r"\$\{assetId\}", "{asset_id}", template)
    assert template in {route.path for route in create_app().routes}

    workspace = APP_JS.read_text(encoding="utf-8")
    assert 'id="ai-mod-asset"' in workspace
    assert "aiModAsset" in workspace


def test_the_monitor_and_the_offset_rule_are_executed_for_every_state():
    """The over-render pair's client half, run rather than read.

    `effectiveOffset` is the client's one copy of the rule the assembly route resolves
    from the same two fields (`latest_take_lead + trim_nudge`); a Monitor previewing one
    slice while assembly cuts another would make the fine-tune a lie, so the formula is
    asserted against the same samples on both sides, and the route's source is scanned
    for the exact expression so a drift is a named failure rather than a silent split.
    """
    samples = [
        {"latest_take_lead": 0.25, "trim_nudge": 0.0},
        {"latest_take_lead": 0.25, "trim_nudge": -0.25},
        {"latest_take_lead": 0.0, "trim_nudge": 0.125},
        {"latest_take_lead": 0.7083333, "trim_nudge": -0.5},
        {},  # a legacy shot: both fields absent read as 0
    ]
    states = run_module(f"""
      import {{ effectiveOffset, monitorShotAt, monitorState, takeAudioControl,
        trimNudgeControl }} from './src/music_video_producer/web/assets/api.js';
      const samples = {json.dumps(samples)};
      const shots = [
        {{ id: 'a', start: 0, duration: 4, latest_output: 'shots/a.mp4',
           latest_take_lead: 0.25, trim_nudge: 0.125 }},
        {{ id: 'b', start: 4, duration: 4 }},
        {{ id: 'c', start: 8, duration: 2, latest_output: 'shots/c.mp4',
           mix_take_audio: true }},
      ];
      const project = {{ shots }};
      console.log(JSON.stringify({{
        offsets: samples.map(effectiveOffset),
        control: trimNudgeControl(shots[0]),
        controlBare: trimNudgeControl(shots[1]),
        inA: monitorState(project, 1.0),
        boundary: monitorState(project, 4.0),
        noTake: monitorState(project, 5.0),
        accepted: monitorState(project, 8.5),
        gap: monitorState(project, 11.5),
        nothing: monitorState(undefined, 0),
        atShotEnd: monitorShotAt(project, 11.0),
        audioControl: takeAudioControl(shots[2]),
        audioControlOff: takeAudioControl(shots[0]),
        audioControlBare: takeAudioControl(shots[1]),
      }}));
    """)

    for sample, offset in zip(samples, states["offsets"]):
        expected = sample.get("latest_take_lead", 0) + sample.get("trim_nudge", 0)
        assert offset == pytest.approx(expected), sample

    # The take view folds the offset in: playhead 1.0 in a shot starting at 0 with
    # offset 0.375 previews 1.375 s into the take — the slice assembly will cut.
    assert states["inA"]["kind"] == "take"
    assert states["inA"]["takeTime"] == pytest.approx(1.375)
    # A boundary belongs to the shot it opens, matching the cumulative grid.
    assert states["boundary"]["kind"] == "no-take"
    assert states["boundary"]["shot"]["id"] == "b"
    assert states["noTake"]["label"]
    assert states["gap"]["kind"] == "gap"
    assert states["nothing"]["kind"] == "gap"
    assert states["atShotEnd"] is None

    # The acceptance flag reaches the preview: an unaccepted take is muted, an accepted
    # one is not — the same field assembly mixes by, so preview and export agree.
    assert states["inA"]["muted"] is True
    assert states["accepted"]["kind"] == "take"
    assert states["accepted"]["muted"] is False
    assert states["audioControl"] == {"shown": True, "checked": True}
    assert states["audioControlOff"] == {"shown": True, "checked": False}
    assert states["audioControlBare"]["shown"] is False

    # The nudge control shows only with a take, and floors at the recorded lead.
    assert states["control"] == {
        "shown": True, "lead": 0.25, "nudge": 0.125, "offset": 0.375, "minNudge": -0.25,
    }
    assert states["controlBare"]["shown"] is False

    # The server's half of the contract: the route resolves the identical expression.
    route_source = Path("src/music_video_producer/app.py").read_text(encoding="utf-8")
    assert "offset=shot.latest_take_lead + shot.trim_nudge" in route_source

    # And the Monitor is wired where every playhead move already passes: position changes
    # and transport changes both reach it, and the markup exists to receive it.
    workspace = APP_JS.read_text(encoding="utf-8")
    playhead_fn = workspace.split("function updateTimelinePlayhead()", 1)[1].split("\nfunction", 1)[0]
    assert "syncMonitor();" in playhead_fn
    transport_fn = workspace.split("function syncTransportState()", 1)[1].split("\nfunction", 1)[0]
    assert "syncMonitor();" in transport_fn
    markup = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="monitor-overlay"' in markup
    monitor_tag = re.search(r'<video[^>]*id="monitor-video"[^>]*>', markup)
    assert monitor_tag, "the Monitor's video element left the markup"
    # Muted by default: the master song is the timeline's sound, and muted playback is
    # what browsers allow to start programmatically. `syncMonitor` un-mutes per shot from
    # the acceptance flag — the same field assembly mixes by.
    assert "muted" in monitor_tag.group(0)
    assert "video.muted = view.muted;" in workspace

    # The two line mutes exist, are wired, and are session-only: no field name for them
    # may appear anywhere in the persisted model.
    assert 'id="mute-song"' in markup and 'id="mute-video"' in markup
    assert '$("#mute-song").addEventListener' in workspace
    assert '$("#mute-video").addEventListener' in workspace
    models_source = Path("src/music_video_producer/models.py").read_text(encoding="utf-8")
    assert "line_muted" not in models_source and "songLineMuted" not in models_source

    # The acceptance checkbox writes the one persisted field through the ordinary save.
    assert 'id="mix-take-audio"' in workspace
    assert "shot.mix_take_audio = event.target.checked;" in workspace


def test_the_shot_inspector_draws_the_player_and_approval_pair_from_the_shot_fields():
    """The player and the pair, rendered and clicked against the workspace's own code.

    The player's presence is decided from `shot.latest_output` and from nothing else — the same
    rule `updateShotFromInspector` records for the expansion box, and the mutation that matters:
    a stub DOM cannot tell an absent element from an empty one, so the assertion is on the markup
    itself, which must contain no `<video` at all for a shot with no take. Its `src` must carry
    ids only; a `latest_output` that travelled into the URL would be the client choosing the
    path, which is exactly what the serve-by-ids route exists to prevent (the pointer may appear
    in the query string only, as a cache key the server never reads).

    The clicks prove the pair reaches the two bodyless routes — never the generic shots write —
    in the direction the decision that drew the button carried.
    """
    rendered = run_workspace("""
      const project = (fields) => ({
        id: 'p1', assets: [], jobs: [], song: null,
        shots: [{ id: 'shot_a', start: 0, duration: 5, prompt: 'A singer turns toward camera',
                  mode: 'text', asset_ids: [], reference_labels: {}, use_song_audio: false,
                  seed: 0, status: 'complete', prompt_id: 'p-1', latest_output: 'takes/one.mp4',
                  approved_output: '', locked: false, ...fields }],
      });
      const draw = (fields) => {
        state.project = project(fields);
        state.selectedShotId = 'shot_a';
        app.renderShotInspector();
        const html = at('#shot-inspector').innerHTML;
        return {
          video: html.includes('<video'),
          player: html.includes('id="take-player"'),
          src: (html.match(/src="([^"]*)"/) || [])[1] || '',
          button: html.includes('id="approve-take"'),
          disabled: /id="approve-take"[^>]*\\sdisabled/.test(html),
          html,
        };
      };
      const complete = draw({});
      const unrendered = draw({ status: 'draft', prompt_id: '', latest_output: '' });
      const approved = draw({ status: 'approved', approved_output: 'takes/one.mp4' });
      const inFlight = draw({ status: 'queued' });

      const clicked = async (fields, reply) => {
        draw(fields);
        globalThis.fetch = (path, options = {}) => {
          requests.push({ path, method: options.method || 'GET', body: options.body || null });
          return Promise.resolve({
            ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => reply,
          });
        };
        requests.length = 0;
        toasts.length = 0;
        await fire('#approve-take:click', {});
        return {
          requests: [...requests],
          status: state.project.shots[0].status,
          approvedOutput: state.project.shots[0].approved_output,
          toasts: toasts.map((item) => item.textContent),
        };
      };
      const toasts = [];
      globalThis.document.createElement = () => { const item = make('<toast>'); toasts.push(item); return item; };
      const approving = await clicked({}, project({ status: 'approved', approved_output: 'takes/one.mp4' }));
      const unapproving = await clicked(
        { status: 'approved', approved_output: 'takes/one.mp4' }, project({}));

      console.log(JSON.stringify({
        complete: { video: complete.video, player: complete.player, src: complete.src,
                    button: complete.button, disabled: complete.disabled,
                    label: complete.html.includes(contract.APPROVE_LABEL) },
        unrendered: { video: unrendered.video, player: unrendered.player, button: unrendered.button },
        approved: { video: approved.video, label: approved.html.includes(contract.UNAPPROVE_LABEL) },
        inFlight: { button: inFlight.button, disabled: inFlight.disabled,
                    reason: inFlight.html.includes(contract.APPROVE_IN_FLIGHT) },
        approving, unapproving,
        approveNotice: contract.approvalNotice(project({}), 'shot_a', 'approve'),
        unapproveNotice: contract.approvalNotice(project({}), 'shot_a', 'unapprove'),
      }));
    """)

    # The player, from the field: drawn with a take, absent — not empty, absent — without one.
    assert rendered["complete"]["video"] is True
    assert rendered["complete"]["player"] is True
    assert rendered["unrendered"]["video"] is False
    assert rendered["unrendered"]["player"] is False
    assert rendered["unrendered"]["button"] is False
    # Ids only in the path; the pointer rides in the query string as a cache key and the path
    # itself names nothing a client chose.
    src = rendered["complete"]["src"]
    assert src.split("?")[0] == "/api/projects/p1/shots/shot_a/take"
    assert "takes/one.mp4" not in src.split("?")[0]
    # The approved shot still shows its player: un-approve is decided while watching too.
    assert rendered["approved"]["video"] is True

    # The pair: approve on a settled take, un-approve on an approved one, refused in flight.
    assert rendered["complete"]["button"] is True
    assert rendered["complete"]["disabled"] is False
    assert rendered["complete"]["label"] is True
    assert rendered["approved"]["label"] is True
    assert rendered["inFlight"] == {"button": True, "disabled": True, "reason": True}

    # Each direction is one bodyless request to its own route, never the generic shots write.
    assert rendered["approving"]["requests"] == [
        {"path": "/api/projects/p1/shots/shot_a/approve", "method": "POST", "body": None}
    ]
    assert rendered["unapproving"]["requests"] == [
        {"path": "/api/projects/p1/shots/shot_a/unapprove", "method": "POST", "body": None}
    ]
    for direction in ("approving", "unapproving"):
        assert not any(
            sent["path"].endswith("/shots") for sent in rendered[direction]["requests"]
        ), direction

    # The reply is adopted and the Director is told the consequence in the server's sentence.
    assert rendered["approving"]["status"] == "approved"
    assert rendered["approving"]["approvedOutput"] == "takes/one.mp4"
    assert rendered["unapproving"]["status"] == "complete"
    assert rendered["unapproving"]["approvedOutput"] == ""
    assert rendered["approving"]["toasts"] == [rendered["approveNotice"]]
    assert rendered["unapproving"]["toasts"] == [rendered["unapproveNotice"]]
    assert "cannot be re-rendered" in rendered["approveNotice"]
    assert "Nothing was deleted" in rendered["unapproveNotice"]
    assert "SHOT 01 (shot_a)" in rendered["approveNotice"]


def test_approval_wordings_are_the_servers_own():
    """One rule, one sentence, whichever side the Director meets it on — the render-again
    convention applied to the approval pair: the in-flight refusal is previewed by the panel in
    the server's words, and both toasts are the server's own account of what each direction did.
    """
    from music_video_producer.app import (
        APPROVE_IN_FLIGHT_REFUSAL,
        APPROVE_NOTICE,
        UNAPPROVE_NOTICE,
    )

    shared = run_module("""
      import { APPROVE_IN_FLIGHT, APPROVE_NOTICE, UNAPPROVE_NOTICE, approvalNotice }
        from './src/music_video_producer/web/assets/api.js';
      const project = { shots: [{ id: 'shot_a' }, { id: 'shot_b' }] };
      console.log(JSON.stringify({
        inFlight: APPROVE_IN_FLIGHT,
        approve: APPROVE_NOTICE,
        unapprove: UNAPPROVE_NOTICE,
        approveNotice: approvalNotice(project, 'shot_b', 'approve'),
        unapproveNotice: approvalNotice(project, 'shot_b', 'unapprove'),
      }));
    """)

    # "this shot" stands in for the label the server prefixes, mid-sentence and so lower-case;
    # every other word has to be the same or the two sides describe different rules.
    assert shared["inFlight"] == APPROVE_IN_FLIGHT_REFUSAL.format(shot="this shot")
    assert shared["approve"] == APPROVE_NOTICE
    assert shared["unapprove"] == UNAPPROVE_NOTICE
    assert shared["approveNotice"] == APPROVE_NOTICE.format(shot="SHOT 02 (shot_b)")
    assert shared["unapproveNotice"] == UNAPPROVE_NOTICE.format(shot="SHOT 02 (shot_b)")


def test_neither_refusal_is_decided_by_its_status_code_in_the_browser():
    """Both codes moved to 409 on 2026-08-18. This is the test that says nothing broke by it.

    The renumbering was safe only because every client half recognises its own refusal by a
    substring of the server's sentence, and that was a claim about the code rather than something
    the suite executed. So it is executed here, and from both sides of the change: each refusal is
    delivered to the booted workspace at **409 and at 422**, and the observable result — what was
    toasted, what was requested next, and what the local shot status became — has to be identical.
    A client that grew a branch on the code fails this whichever way the branch went, and it fails
    it at the moment the branch is written rather than the next time a code moves.

    That identity is only worth having with the *server's* wording in it, so the mark-ready half
    takes its sentence and its code straight from `mark_ready_refusal` rather than from a literal
    typed here.

    The last probe is the mechanism underneath: `request` throws a bare `Error` carrying the
    server's sentence and no status at all, so a status branch is not something a handler could
    write today without changing `request` first. Asserted over several spellings, because
    `error.status` is only the obvious one.
    """
    from music_video_producer.app import mark_ready_refusal
    from music_video_producer.models import Project as ServerProject
    from music_video_producer.models import Shot as ServerShot

    # The server's own answer for a live render, taken from the guard rather than restated.
    in_flight = ServerProject(name="Live")
    in_flight.shots = [
        ServerShot(id="shot_a", start=0, duration=5, prompt="A singer turns", status="queued")
    ]
    refusal = mark_ready_refusal(in_flight, in_flight.shots[0], target="draft")
    assert refusal is not None
    mark_code, mark_sentence = refusal
    assert mark_code == 409, "the mark's in-flight refusal is a state conflict, as render-again's is"

    song_sentence = SONG_CONTEXT_RESTORE_REFUSAL.format(field=SONG_CONTEXT_LABELS["lyrics"])

    result = run_workspace(f"""
      const songSentence = {json.dumps(song_sentence)};
      const markSentence = {json.dumps(mark_sentence)};
      const withSong = () => ({{
        id: 'p1', assets: [], jobs: [], shots: [],
        song: {{ title: 'Spine', source: 'imported', path: 'media/songs/000-master.wav',
                duration: 180, lyrics: 'live sheet', caption: 'live style',
                lyrics_previous: 'the kept sheet', caption_previous: null }},
      }});
      const withShot = () => ({{
        id: 'p1', assets: [], jobs: [], song: null,
        shots: [{{ id: 'shot_a', start: 0, duration: 5, prompt: 'A singer turns toward camera',
                  mode: 'text', asset_ids: [], reference_labels: {{}}, use_song_audio: false,
                  seed: 0, status: 'draft', prompt_id: '', latest_output: '',
                  approved_output: '', locked: false }}],
      }});
      const toasts = [];
      globalThis.document.createElement = () => {{ const item = make('<toast>'); toasts.push(item); return item; }};
      // Everything but the refused path answers 200 with a fresh project, so the stale-state
      // refresh a refusal may trigger is something this can observe rather than something that
      // rejects and hides the difference.
      const serve = (refused, status, detail, refreshed) => {{
        globalThis.fetch = (url, options = {{}}) => {{
          requests.push({{ path: url, method: options.method || 'GET', body: options.body || null }});
          const isRefused = url === refused;
          const code = isRefused ? status : 200;
          return Promise.resolve({{
            ok: code < 400, status: code, statusText: 'canned',
            headers: {{ get: () => 'application/json' }},
            json: async () => (isRefused ? {{ detail }} : refreshed),
          }});
        }};
      }};
      const restoreUnder = async (status) => {{
        state.project = withSong();
        state.songContextDirty = false;
        app.renderSong();
        serve('/api/projects/p1/song/context/lyrics/restore', status, songSentence, withSong());
        requests.length = 0; toasts.length = 0;
        await fire('#restore-song-lyrics:click', {{}});
        await flush();
        return {{ toasts: toasts.map((item) => item.textContent), paths: requests.map((sent) => sent.path) }};
      }};
      const markUnder = async (status) => {{
        state.project = withShot();
        state.selectedShotId = 'shot_a';
        app.renderShotInspector();
        serve('/api/projects/p1/shots/shot_a/mark-ready', status, markSentence, withShot());
        requests.length = 0; toasts.length = 0;
        await fire('#mark-ready:click', {{}});
        await flush();
        return {{
          toasts: toasts.map((item) => item.textContent),
          paths: requests.map((sent) => sent.path),
          status: state.project.shots[0].status,
        }};
      }};
      const seen = async () => {{
        serve('/probe', 409, songSentence, {{}});
        try {{
          await contract.request('/probe', {{ method: 'POST' }});
          return 'NO THROW';
        }} catch (error) {{
          return {{
            message: error.message,
            keys: Object.keys(error),
            status: error.status ?? null,
            statusCode: error.statusCode ?? null,
            code: error.code ?? null,
            response: error.response ?? null,
          }};
        }}
      }};
      console.log(JSON.stringify({{
        restoreAt409: await restoreUnder(409), restoreAt422: await restoreUnder(422),
        markAt409: await markUnder(409), markAt422: await markUnder(422),
        seen: await seen(),
      }}));
    """)

    # The whole claim, in two lines: the code the server picks makes no difference to any of it.
    assert result["restoreAt409"] == result["restoreAt422"], result
    assert result["markAt409"] == result["markAt422"], result

    # ...and what it does under both is the right thing, or "identical" would be satisfied by two
    # identically broken runs. The restore toasts the server's sentence and then refreshes, because
    # a refusal against a button that should have been disabled means this client is stale.
    assert result["restoreAt409"]["toasts"] == [song_sentence]
    assert result["restoreAt409"]["paths"] == [
        "/api/projects/p1/song/context/lyrics/restore",
        "/api/projects/p1",
    ]
    # The mark toasts the server's sentence and stops. There is nothing stale about a live render,
    # so there is nothing to refresh, and the shot must not move locally on a refusal.
    assert result["markAt409"]["toasts"] == [mark_sentence]
    assert result["markAt409"]["paths"] == ["/api/projects/p1/shots/shot_a/mark-ready"]
    assert result["markAt409"]["status"] == "draft"

    # The mechanism: the status never reaches a handler in any spelling, so a branch on it is not
    # something that could be written by accident.
    assert result["seen"]["message"] == song_sentence
    assert result["seen"]["keys"] == []
    for spelling in ("status", "statusCode", "code", "response"):
        assert result["seen"][spelling] is None, spelling


def test_mark_ready_wordings_are_the_servers_own():
    """One rule, one sentence, whichever side the Director meets it on.

    The lock and the approval are refused by the routes and previewed by the panel, and the two
    notices are the server's account of what each direction did. Two hand-written wordings for one
    rule is how the browser starts describing behaviour the server no longer has.
    """
    from music_video_producer.app import (
        MARK_DRAFT_NOTICE,
        MARK_READY_APPROVED_REFUSAL,
        MARK_READY_LOCKED_REFUSAL,
        MARK_READY_NOTICE,
    )

    shared = run_module("""
      import { MARK_DRAFT_NOTICE, MARK_READY_APPROVED, MARK_READY_LOCKED, MARK_READY_NOTICE,
        markReadyNotice } from './src/music_video_producer/web/assets/api.js';
      const project = { shots: [{ id: 'shot_a' }, { id: 'shot_b' }, { id: 'shot_c' }] };
      console.log(JSON.stringify({
        locked: MARK_READY_LOCKED,
        approved: MARK_READY_APPROVED,
        readyNotice: MARK_READY_NOTICE,
        draftNotice: MARK_DRAFT_NOTICE,
        ready: markReadyNotice(project, 'shot_c', 'ready'),
        draft: markReadyNotice(project, 'shot_c', 'draft'),
        // An unknown direction falls to the arming sentence rather than to an empty toast: a
        // success the Director is told nothing about is worse than one described imprecisely.
        fallback: markReadyNotice(project, 'shot_a', undefined),
      }));
    """)

    # The refusals are the server's sentence exactly, with "This shot" standing in for the label the
    # server prefixes: the panel is already showing the shot, so naming it there would name it
    # twice, and every other word has to match or the two sides describe different rules.
    assert shared["locked"] == MARK_READY_LOCKED_REFUSAL.format(shot="This shot")
    assert shared["approved"] == MARK_READY_APPROVED_REFUSAL.format(shot="This shot")
    assert shared["readyNotice"] == MARK_READY_NOTICE
    assert shared["draftNotice"] == MARK_DRAFT_NOTICE
    assert shared["ready"] == MARK_READY_NOTICE.format(shot="SHOT 03 (shot_c)")
    assert shared["draft"] == MARK_DRAFT_NOTICE.format(shot="SHOT 03 (shot_c)")
    assert shared["fallback"] == MARK_READY_NOTICE.format(shot="SHOT 01 (shot_a)")


def test_the_shot_inspector_does_not_re_decide_the_mark_ready_control():
    """Source-level companion: the template applies the decision and never re-makes it.

    The executed test above proves the decision is right and is used. This is what keeps it the
    only copy -- a second status or prompt test written into the template is a second rule, and the
    one that is tested is not the one that would then be drawn. The click handler is included
    because the *direction* is the half most likely to be re-derived: `shot.status === "ready"` at
    the fetch site reads as harmless and is a second opinion about which route to call.
    """
    inspector = APP_JS.read_text(encoding="utf-8").split(
        "export function renderShotInspector", 1
    )[1].split("\n}", 1)[0]
    body = without_comments(inspector)

    assert "const mark = markReadyControl(shot);" in body
    assert "mark.shown" in body
    assert 'mark.disabled ? "disabled" : ""' in body
    assert "escapeHtml(mark.title)" in body
    assert "escapeHtml(mark.label)" in body
    # The direction is carried out of the decision, not recomputed at the click.
    assert 'mark.action === "draft"' in body
    assert "api.markShotDraft(projectId, shot.id)" in body
    assert "api.markShotReady(projectId, shot.id)" in body
    # No second copy of the decision, in any of its spellings.
    for redecided in ("shot.locked", "approved_output", 'shot.status === "ready"', "promptRejection"):
        assert redecided not in body, redecided


def test_the_shot_inspector_does_not_re_decide_the_render_again_control():
    """Source-level companion: the template applies the decision and never re-makes it.

    The executed test above proves the decision is right and is used. This is what keeps it the
    only copy -- a second lock or prompt test written into the template is a second rule, and the
    one that is tested is not the one that would then be drawn.
    """
    inspector = APP_JS.read_text(encoding="utf-8").split(
        "export function renderShotInspector", 1
    )[1].split("\n}", 1)[0]
    body = without_comments(inspector)

    assert "const again = renderAgainControl(shot);" in body
    assert "again.shown" in body
    assert 'again.disabled ? "disabled" : ""' in body
    assert "escapeHtml(again.title)" in body
    # No second copy of the decision, in any of its spellings.
    for redecided in ("shot.locked", "approved_output", 'status === "complete"'):
        assert redecided not in body, redecided


def selector_specificity(selector: str) -> tuple[int, int, int]:
    """(ids, classes, elements) for a simple selector, which is all this stylesheet uses."""
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+", selector))
    elements = len(re.findall(r"(?:^|[\s>+~])([a-zA-Z][\w-]*)", selector))
    return ids, classes, elements


def css_rules(css: str) -> list[tuple[str, str]]:
    """Every rule in declaration order, as (selector, body). Comments are dropped first, or a
    rule's explanation would be read as part of its selector."""
    without = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [
        (match.group(1).strip(), match.group(2))
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", without)
    ]


def test_the_blocked_clip_rule_keeps_the_order_and_the_token_its_comment_claims():
    """The comment states an ordering dependency the old assertion could not see.

    All that was pinned was that `border-style` appears somewhere in the rule -- so moving the
    rule above `.selected`, which a reorder or a minifier does silently, left a selected blocked
    clip drawing the acid border and the suite green. Equal specificity is what makes source order
    decide, so both halves of the claim are asserted.

    The colour is a token too: the same literal hex was written out here and on `.job-status.error`
    with a palette token in the very next rule, which is how one of the two drifts.
    """
    css = STYLES_CSS.read_text(encoding="utf-8")
    rules = css_rules(css)
    # Every rule that paints a clip's border and could apply to a blocked, selected clip.
    contenders = [
        (index, selector, declarations)
        for index, (selector, declarations) in enumerate(rules)
        if "border-color" in declarations
        and set(re.findall(r"\.([\w-]+)", selector)) <= {"shot-clip", "selected", "no-prompt"}
        and "shot-clip" in selector
    ]
    selectors = [selector for _, selector, _ in contenders]
    assert ".shot-clip.selected" in selectors, selectors
    assert ".shot-clip.no-prompt" in selectors, selectors

    blocked = next(item for item in contenders if item[1] == ".shot-clip.no-prompt")
    selected = next(item for item in contenders if item[1] == ".shot-clip.selected")
    # Declared after `.selected`, so a blocked clip stays visibly blocked while selected...
    assert selected[0] < blocked[0], selectors
    # ...which only works because neither selector outranks the other.
    assert selector_specificity(".shot-clip.no-prompt") == selector_specificity(".shot-clip.selected")
    # And nothing declared later reclaims the border from it.
    assert blocked[0] == max(index for index, _, _ in contenders), selectors

    # Border *style*, not only a border colour: the .job-status precedent, and the reason the
    # flag text exists as well. Either signal alone would be one signal.
    assert "border-style" in blocked[2], blocked[2]
    # No invented colour: every colour in the rule is a palette token, as `.message.system` is.
    assert not re.search(r"#[0-9a-fA-F]{3,8}", blocked[2]), blocked[2]
    root = re.search(r":root\s*\{(.*?)\n\}", css, re.DOTALL)
    assert root, "styles.css no longer declares its palette on :root"
    tokens = re.findall(r"var\((--[\w-]+)\)", blocked[2])
    assert tokens, blocked[2]
    for token in tokens:
        assert f"{token}:" in root.group(1), f"{token} is not a palette token"
    # And the hex that token replaced is gone from the file, not merely from this rule.
    assert "#7f3732" not in css.replace(f"{tokens[0]}: #7f3732;", ""), "the literal is still spelled out"


def server_readiness_report(tmp_path: Path) -> dict:
    """A readiness report exactly as the browser receives it: built by the server, read off the route.

    Nothing tied the client's parser to a real report before this. The wording test feeds it
    hand-written literals and the route test asserts an empty warnings list, so renaming
    `blocking`, `warnings` or `shot_ids` on the server's report left every JavaScript test green
    while the browser silently stopped seeing a single blocked Shot.
    """
    from fastapi.testclient import TestClient

    from music_video_producer.app import create_app
    from music_video_producer.config import Settings
    from music_video_producer.store import ProjectStore

    store = ProjectStore(tmp_path)
    project = store.create(Project(name="Readiness"))
    # Deliberately in an order the song does not follow: the report is ordered by time while the
    # labels number by manifest position, so a client that rebuilt the numbering from its own shot
    # array would disagree with the server about which clip a note is about.
    project.shots = [
        Shot(id="shot_echo", start=10, duration=5, prompt="a  SINGER   turns toward camera"),
        # Same prompt as the one above once case and spacing are ignored: a real sameness warning.
        Shot(id="shot_written", start=0, duration=5, prompt="A singer turns toward camera"),
        Shot(id="shot_blank", start=5, duration=5, prompt="   \n\t"),
    ]
    store.save(project)
    app = create_app(
        settings=Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy"), store=store
    )
    response = TestClient(app).get(f"/api/projects/{project.id}/readiness")

    assert response.status_code == 200, response.text
    return response.json()


def test_the_client_readiness_parsers_are_executed_against_a_real_server_report(tmp_path: Path):
    """Every client extractor, run over one report the server really produced.

    The ids, the notes and the counts all come from the route rather than from a literal written
    to match it, so a rename on either side lands here instead of in a browser nobody tests.
    """
    from music_video_producer.batch import SHOT_WITHOUT_PROMPT, SHOTS_SHARE_ONE_PROMPT

    report = server_readiness_report(tmp_path)
    script = f"""
      import {{ blockedShotIds, blockedShotLabels, generateAllPlan,
        readinessLines, readinessSummary }}
        from './src/music_video_producer/web/assets/api.js';
      const report = {json.dumps(report)};
      const shots = [{{ id: 'shot_written', status: 'ready' }}, {{ id: 'shot_blank', status: 'ready' }}];
      console.log(JSON.stringify({{
        blocked: blockedShotIds(report),
        labels: blockedShotLabels(report),
        lines: readinessLines(report),
        summary: readinessSummary(report),
        button: generateAllPlan({{ shots }}, report),
      }}));
    """

    parsed = run_module(script)
    # Read off the report rather than written out again, so the labelling scheme stays the
    # server's: the notes are in song order while the names number by manifest position.
    blocked_labels = [label for note in report["blocking"] for label in note["labels"]]
    assert blocked_labels == ["SHOT 03 (shot_blank)"], report["blocking"]

    # The blocking half: the one Shot with no prompt, named as the server names it.
    assert parsed["blocked"] == ["shot_blank"]
    assert parsed["labels"] == blocked_labels
    # The button stays enabled — the server-side batch (FR-4) skips the blocked shot by
    # name and submits the rest — but the heads-up is in the title before the click.
    assert parsed["button"]["disabled"] is False
    assert parsed["button"]["blocked"] == ["shot_blank"]
    assert "1 will be skipped" in parsed["button"]["title"]

    # The sameness half, which nothing in the browser read at all before this.
    blocking = [line for line in parsed["lines"] if line["kind"] == "blocking"]
    warnings = [line for line in parsed["lines"] if line["kind"] == "warning"]
    assert [line["shotIds"] for line in blocking] == [["shot_blank"]]
    assert [line["shotIds"] for line in warnings] == [["shot_written", "shot_echo"]]
    # The server's own sentences, passed through rather than reworded on the client.
    assert blocking[0]["reason"] == SHOT_WITHOUT_PROMPT
    assert warnings[0]["reason"] == SHOTS_SHARE_ONE_PROMPT
    assert SHOTS_SHARE_ONE_PROMPT in warnings[0]["text"]
    # Named by the report's own labels -- `SHOT 02 (id)` -- so the pair can be found on screen. The
    # numbers are not ascending here on purpose: rebuilding them from the client's shot array would
    # produce "Shot 01 and Shot 02" for this pair and point at the wrong clips.
    assert warnings[0]["shots"] == ["SHOT 02 (shot_written)", "SHOT 01 (shot_echo)"]
    assert "SHOT 02 (shot_written) and SHOT 01 (shot_echo)" in warnings[0]["text"]
    assert blocking[0]["shots"] == blocked_labels
    # Which half a line came from is in the words, not only in the colour of its marker.
    assert warnings[0]["text"].startswith("Near-duplicate")
    assert blocking[0]["text"].startswith("Blocked")

    # And the counts are the report's, not recounted from the client's copy of the plan.
    assert report["shot_count"] == 3
    assert report["ready_count"] == 2
    assert report["warnings_computed"] is True
    assert "2 of 3 shots have a prompt" in parsed["summary"]
    assert "1 cannot be submitted" in parsed["summary"]
    assert "1 near-duplicate pair" in parsed["summary"]


def server_stale_readiness_report(tmp_path: Path) -> tuple[dict, dict]:
    """A real report over a plan with one stale reference map, and the project it is about.

    Both come off the routes rather than out of a literal, for `server_readiness_report`'s reason:
    the note's `kind` is the only thing the browser draws from, and a report built by hand would
    keep this test green through a rename on the wire.

    The stale shot is the **prose** shape -- its expansion carries the reference map in its own
    first line and now cites a picture that line does not name -- which is the shape the Director's
    own project is made of. Written straight onto the manifest because `refresh_reference_maps`
    exists precisely to stop this state arriving through a route for a prose shot; the document
    shape, which does arrive through a route, is driven end to end in `tests/test_api.py`.
    """
    from fastapi.testclient import TestClient

    from music_video_producer.app import create_app
    from music_video_producer.config import Settings
    from music_video_producer.reference_map import reference_map_sentence
    from music_video_producer.store import ProjectStore

    store = ProjectStore(tmp_path)
    project = store.create(Project(name="Stale maps"))
    project.assets = [
        Asset(id="asset_bed", name="Dusk Warehouse Bed", kind="setting", path="assets/bed.png"),
        Asset(id="asset_lead", name="HarderFaster sheet", kind="character", path="assets/lead.png"),
    ]
    bed_only = reference_map_sentence(["<Picture 1> is Dusk Warehouse Bed"])
    project.shots = [
        Shot(
            id="shot_fresh", start=0, duration=5, prompt="She turns toward camera.",
            status="ready", h3_prompt=f"{bed_only} She turns toward camera.",
            citations=[AssetCitation(asset_id="asset_bed", role="reference", order=0)],
        ),
        Shot(
            id="shot_stale", start=5, duration=5, prompt="He looks up at the sign.",
            status="ready", h3_prompt=f"{bed_only} He looks up at the sign.",
            citations=[
                AssetCitation(asset_id="asset_bed", role="reference", order=0),
                AssetCitation(asset_id="asset_lead", role="reference", order=1),
            ],
        ),
    ]
    store.save(project)
    client = TestClient(
        create_app(
            settings=Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy"), store=store
        )
    )
    report = client.get(f"/api/projects/{project.id}/readiness")
    loaded = client.get(f"/api/projects/{project.id}")

    assert report.status_code == 200, report.text
    assert loaded.status_code == 200, loaded.text
    return report.json(), loaded.json()


def test_the_readiness_list_draws_a_stale_reference_map_under_its_own_name(tmp_path: Path):
    """The new note kind, rendered by the workspace and read out of the markup it produced.

    Not grepped: the region is drawn by a project load, in the stub DOM, from a report the server
    really built -- so a `kind` that failed to reach the wire, a label table that failed to match
    it, or a line that lost its list-marker class all land here.

    Two things are asserted about the line, and they are different claims. It reads under its own
    heading, because "Blocked" over both blocks would make an empty prompt and a stale map look
    like one problem with one fix -- they send the Director to two different boxes. And it keeps
    the `blocking` class, because it *is* a refusal and the list marker must not say otherwise.
    """
    from music_video_producer.batch import SHOT_WITH_STALE_REFERENCE_MAP

    report, project = server_stale_readiness_report(tmp_path)
    kinds = {note["kind"] for note in report["blocking"]}
    assert kinds == {"stale_map"}, report["blocking"]

    parsed = run_workspace(
        """
        at('#project-select').value = __ID__;
        await fire('#project-select:change', { target: { value: __ID__ } });
        await flush();
        console.log(JSON.stringify({
          markup: at('#plan-readiness').innerHTML,
          blocked: at('#plan-readiness').classList.contains('blocked'),
        }));
        """.replace("__ID__", json.dumps(project["id"])),
        {
            f"/api/projects/{project['id']}": {"body": project},
            f"/api/projects/{project['id']}/readiness": {"body": report},
        },
    )

    markup = parsed["markup"]
    # One line, under the stale map's own heading, carrying the server's whole sentence.
    assert markup.count("<li") == 1, markup
    assert '<li class="blocking">' in markup, markup
    assert "Stale reference map - SHOT 02 (shot_stale):" in markup, markup
    assert escape_for_markup(SHOT_WITH_STALE_REFERENCE_MAP) in markup, markup
    # The fresh shot is not mentioned at all, and the summary counts what it counts.
    assert "shot_fresh" not in markup
    assert "2 of 2 shots have a prompt" in markup
    assert "1 cannot be submitted" in markup
    assert parsed["blocked"] is True


def escape_for_markup(text: str) -> str:
    """`api.escapeHtml`'s output for a sentence, so an assertion can look for it in innerHTML."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def test_the_batch_confirmation_names_the_reason_each_skipped_shot_will_be_skipped(
    tmp_path: Path,
):
    """"3 will be skipped (no prompt)" was hardcoded, and a stale map is a second block.

    A stale shot has a prompt -- a good one, usually -- so the old sentence would have sent the
    Director to the inspector's intent box for a problem that lives in the expanded-prompt box. The
    noun is read off the blocking note's `kind` now, and both blocks in one batch name both.
    """
    report, _project = server_stale_readiness_report(tmp_path)
    mixed = {
        **report,
        "blocking": [
            *report["blocking"],
            {
                "shot_ids": ["shot_fresh"], "labels": ["SHOT 01 (shot_fresh)"],
                "reason": "This shot has no prompt.", "kind": "prompt",
            },
        ],
    }
    unknown = {
        **report,
        "blocking": [{**report["blocking"][0], "kind": "something_this_client_never_heard_of"}],
    }
    shots = [{"id": "shot_fresh", "status": "ready"}, {"id": "shot_stale", "status": "ready"}]

    parsed = run_module(f"""
      import {{ BATCH_SKIP_NOUNS, BATCH_SKIP_NOUN_UNKNOWN, generateAllPlan }}
        from './src/music_video_producer/web/assets/api.js';
      const shots = {json.dumps(shots)};
      console.log(JSON.stringify({{
        nouns: BATCH_SKIP_NOUNS,
        fallback: BATCH_SKIP_NOUN_UNKNOWN,
        stale: generateAllPlan({{ shots }}, {json.dumps(report)}),
        mixed: generateAllPlan({{ shots }}, {json.dumps(mixed)}),
        unknown: generateAllPlan({{ shots }}, {json.dumps(unknown)}),
        clean: generateAllPlan({{ shots }}, {json.dumps({**report, "blocking": []})}),
      }}));
    """)

    # The kinds are the server's values, not a second vocabulary.
    from music_video_producer.batch import NOTE_KIND_PROMPT, NOTE_KIND_STALE_MAP

    assert set(parsed["nouns"]) == {NOTE_KIND_PROMPT, NOTE_KIND_STALE_MAP}
    assert parsed["stale"]["blocked"] == ["shot_stale"]
    assert "1 will be skipped (stale reference map)" in parsed["stale"]["title"]
    assert "no prompt" not in parsed["stale"]["title"]
    # Both blocks in one batch: both nouns, once each, and the count is still the count.
    assert "2 will be skipped (no prompt, stale reference map)" in parsed["mixed"]["title"]
    assert "2 will be skipped (no prompt, stale reference map)" in parsed["mixed"]["confirm"]
    # A kind this client has never heard of still gets counted and still says something true.
    # The fallback is asserted non-empty first, or the containment below would pass just as
    # happily over "1 will be skipped ()" -- a parenthesis that names nothing reads as a bug.
    assert parsed["fallback"].strip(), "an unknown block must still be named something"
    assert "1 will be skipped (blocked)" in parsed["unknown"]["title"]
    assert f"1 will be skipped ({parsed['fallback']})" in parsed["unknown"]["title"]
    # And a plan with nothing blocked says nothing about skipping, exactly as before.
    assert "skipped" not in parsed["clean"]["title"]
    assert parsed["clean"]["blocked"] == []
    # The button is never disabled by a block: the server-side batch skips by name and submits
    # the rest, and a client-side refusal the route would not make is a refusal nobody can clear.
    assert [parsed[key]["disabled"] for key in ("stale", "mixed", "unknown", "clean")] == [
        False, False, False, False
    ]


def test_an_unchecked_sameness_pass_is_never_reported_as_no_duplicates():
    """`warnings: []` means two different things, and the report says which.

    The submission route asks for a blocking-only report, so an empty list can mean "none found"
    or "we did not look" -- and telling a Director their plan has no near-duplicates on the
    strength of a pass that never ran is the more damaging of the two mistakes. The overflow count
    is the same class of claim: a plan with more pairs than the report lists must not read as a
    plan with exactly as many as it lists.
    """
    from dataclasses import asdict

    from music_video_producer.batch import readiness_report

    project = Project(name="Sameness")
    project.shots = [
        Shot(id="shot_a", start=0, duration=5, prompt="A singer turns toward camera"),
        Shot(id="shot_b", start=5, duration=5, prompt="a  SINGER  turns toward camera"),
    ]
    blocking_only = asdict(readiness_report(project, include_warnings=False))
    full = asdict(readiness_report(project))

    parsed = run_module(f"""
      import {{ SAMENESS_NOT_CHECKED, readinessLines, readinessSummary }}
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({{
        wording: SAMENESS_NOT_CHECKED,
        unchecked: readinessSummary({json.dumps(blocking_only)}),
        checked: readinessSummary({json.dumps(full)}),
        overflowing: readinessSummary({json.dumps({**full, "warnings_omitted": 7})}),
        lines: readinessLines({json.dumps(blocking_only)}),
      }}));
    """)

    # The pass that did not run is said out loud, and never as a count of nothing.
    assert blocking_only["warnings_computed"] is False
    assert blocking_only["warnings"] == []
    assert parsed["wording"] in parsed["unchecked"]
    assert "near-duplicate pair" not in parsed["unchecked"]
    assert parsed["lines"] == []
    # A real pass reports its real count, and says nothing about not having looked.
    assert "1 near-duplicate pair" in parsed["checked"]
    assert parsed["wording"] not in parsed["checked"]
    # And an overflow is counted rather than dropped.
    assert "7 more not listed" in parsed["overflowing"]


def test_an_empty_plans_note_names_no_shot_on_the_client_either(tmp_path: Path):
    """The empty-plan note carries no `shot_ids`, and the client must render it anyway.

    A line built by joining names would render an empty label and a stray separator, and the one
    state where the plan-level sentence is the whole message would be the one that reads broken.
    """
    from fastapi.testclient import TestClient

    from music_video_producer.app import create_app
    from music_video_producer.batch import PLAN_WITHOUT_SHOTS
    from music_video_producer.config import Settings
    from music_video_producer.store import ProjectStore

    store = ProjectStore(tmp_path)
    project = store.create(Project(name="Empty"))
    app = create_app(
        settings=Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy"), store=store
    )
    report = TestClient(app).get(f"/api/projects/{project.id}/readiness").json()

    parsed = run_module(f"""
      import {{ readinessLines, readinessSummary, generateAllPlan }}
        from './src/music_video_producer/web/assets/api.js';
      const report = {json.dumps(report)};
      console.log(JSON.stringify({{
        lines: readinessLines(report),
        summary: readinessSummary(report),
        button: generateAllPlan({{ shots: [] }}, report),
      }}));
    """)

    assert [line["shotIds"] for line in parsed["lines"]] == [[]]
    assert parsed["lines"][0]["text"] == f"Blocked - {PLAN_WITHOUT_SHOTS}"
    assert parsed["summary"].startswith("0 of 0 shots have a prompt")
    assert parsed["button"]["disabled"] is True


def test_the_generate_all_plan_counts_warns_and_never_gates_what_the_server_would_skip():
    """`generateAllPlan` is the button's whole decision, for every state.

    The FR-4 change of stance, recorded: a blocked shot inside the batch no longer
    disables the button — the server-side batch skips it by name and submits the rest —
    but the heads-up still lands *before* the click, in the title and the confirm.
    Replace Existing widens the count to settled unprotected shots; approved and locked
    settled shots are excluded from the count because the server will name them in the
    report rather than re-render them.
    """
    script = """
      import { QUEUE_REPLACE_WITHOUT_TARGETS, QUEUE_WITHOUT_READY_SHOTS, generateAllPlan }
        from './src/music_video_producer/web/assets/api.js';
      const blocking = (...ids) => ({ blocking: ids.map((id) => ({ shot_ids: [id], reason: 'x' })) });
      const shot = (id, status = 'ready', extra = {}) => ({ id, status, ...extra });
      const settledPlan = { shots: [
        shot('shot_a'),
        shot('shot_done', 'complete'),
        shot('shot_err', 'error'),
        shot('shot_appr', 'complete', { approved_output: 'takes/a.mp4' }),
        shot('shot_lock', 'complete', { locked: true }),
        shot('shot_draft', 'draft'),
      ] };
      console.log(JSON.stringify({
        emptyWording: QUEUE_WITHOUT_READY_SHOTS,
        replaceEmptyWording: QUEUE_REPLACE_WITHOUT_TARGETS,
        nothingReady: generateAllPlan({ shots: [] }, blocking()),
        nothingEvenReplacing: generateAllPlan({ shots: [shot('shot_draft', 'draft')] }, null, true),
        two: generateAllPlan({ shots: [shot('shot_a'), shot('shot_b')] }, blocking()),
        one: generateAllPlan({ shots: [shot('shot_a')] }, blocking()),
        blockedInside: generateAllPlan({ shots: [shot('shot_a'), shot('shot_b')] }, blocking('shot_b')),
        blockedElsewhere: generateAllPlan({ shots: [shot('shot_a'), shot('shot_b')] }, blocking('shot_c')),
        noReport: generateAllPlan({ shots: [shot('shot_a')] }, null),
        replacing: generateAllPlan(settledPlan, null, true),
        notReplacing: generateAllPlan(settledPlan, null, false),
      }));
    """

    states = run_module(script)
    assert states["nothingReady"]["disabled"] is True
    assert states["nothingReady"]["title"] == states["emptyWording"]
    assert states["nothingEvenReplacing"]["disabled"] is True
    assert states["nothingEvenReplacing"]["title"] == states["replaceEmptyWording"]
    assert states["two"]["disabled"] is False
    assert states["two"]["count"] == 2
    assert states["two"]["title"] == "Generate 2 H3 shots"
    assert "Queue 2 H3 shots as one batch?" in states["two"]["confirm"]
    assert states["one"]["title"] == "Generate 1 H3 shot"
    # The heads-up before the click, without gating what the server would not gate.
    assert states["blockedInside"]["disabled"] is False
    assert states["blockedInside"]["blocked"] == ["shot_b"]
    assert "1 will be skipped" in states["blockedInside"]["title"]
    assert "1 will be skipped" in states["blockedInside"]["confirm"]
    assert states["blockedElsewhere"]["blocked"] == []
    assert states["noReport"]["disabled"] is False
    # Replace Existing: ready + settled unprotected; approved, locked and draft excluded.
    assert states["replacing"]["count"] == 3
    assert states["notReplacing"]["count"] == 1

    source = APP_JS.read_text(encoding="utf-8")
    jobs = without_comments(app_js_block("function renderJobs"))
    assert "generateAllPlan(state.project, readinessReport" in jobs
    assert '$("#queue-ready").disabled = plan.disabled;' in jobs
    assert '$("#queue-ready").title = plan.title;' in jobs
    # The flagged set's own button redraws from the same pass.
    assert '$("#queue-flagged")' in jobs
    # The handler posts the one server batch and relays its report; no client loop remains.
    assert "api.generateBatch(" in source
    assert "for (const shot of shots) { await api.generateH3" not in source

    # The report is fetched on project load, so the state is known before the Queue panel is even
    # opened -- and abandoned if it lands for a project that is no longer on screen.
    load = source.split("async function loadProject(id) {", 1)[1].split("\n}", 1)[0]
    assert "loadReadiness(id);" in load
    assert "readinessReport = null;" in load
    assert load.index("readinessReport = null;") < load.index("if (!id)")
    fetch = without_comments(app_js_block("async function loadReadiness"))
    assert "api.readiness(projectId)" in fetch
    assert "if (revision !== readinessLoadRevision || state.project?.id !== projectId) return;" in fetch
    assert fetch.index("api.readiness(") < fetch.index("readinessReport = report;")
    # A failed GET is not an error the Director asked for: the route is still the gate.
    assert "catch {" in fetch
    assert "toast(" not in fetch, fetch

    # The markup's own disabled title is the same sentence app.js will set, or the button explains
    # itself differently before and after the first render.
    markup = INDEX_HTML.read_text(encoding="utf-8")
    button = re.search(r'<button[^>]*id="queue-ready"[^>]*>', markup)
    assert button, "the batch action has no button for app.js to bind"
    assert f'title="{states["emptyWording"]}"' in button.group(0), button.group(0)


def test_the_near_duplicate_warnings_reach_a_surface_the_director_can_act_on():
    """`report.warnings` reached nothing at all: half the feature was invisible.

    The queue handler reads only the blocking ids and the compile toast prints the timeline's own
    frame warnings, so the near-duplicate pairs the server computes were never rendered anywhere.
    FR-26 says the Director may "differentiate or accept them deliberately", which is not a choice
    anyone can make about a pair they cannot see.
    """
    markup = INDEX_HTML.read_text(encoding="utf-8")

    # A region of its own, above the batch button that acts on it, announced when it changes:
    # it is rewritten by a fetch the Director did not trigger.
    region = re.search(r'<div class="plan-readiness" id="plan-readiness"[^>]*>', markup)
    assert region, "the readiness report has nowhere to render"
    assert "aria-live" in region.group(0), region.group(0)
    queue_panel = markup.split('id="panel-queue"', 1)[1]
    assert 'id="plan-readiness"' in queue_panel.split('id="queue-layout"', 1)[0]

    render = without_comments(app_js_block("function renderReadiness"))
    assert '$("#plan-readiness")' in render
    # Both halves of the report, and the summary that makes the counts readable at a glance.
    assert "readinessLines(readinessReport)" in render
    assert "readinessSummary(readinessReport)" in render
    # The kind reaches the stylesheet, and the text says it too -- state is never colour alone.
    assert 'line.kind' in render
    assert "escapeHtml(line.text)" in render
    # Drawn on every render of the workspace, not only when a fetch happens to land.
    assert "renderReadiness();" in app_js_block("function renderAll")

    # A stale report is worse than none: the prompts it is about change under it.
    assert "loadReadiness(projectId);" in app_js_block("async function expandShotPrompts")
    assert "loadReadiness(projectId);" in app_js_block("function saveShotsSilently")
    # And the region really is styled, or "surfaced" means a line of unreadable text.
    styles = STYLES_CSS.read_text(encoding="utf-8")
    assert re.search(r"\.plan-readiness \{", styles), "the readiness region has no style of its own"


def test_the_shot_inspector_shows_the_block_the_refusal_sends_the_director_to_fix():
    """"Write a prompt in the shot inspector" led to a panel that said nothing was wrong.

    The inspector is the place the refusal names, so it has to show which Shot is blocked and what
    that means -- and it is where a prompt is edited, so it is also where a near-duplicate pair has
    to be named for the Director to differentiate or accept it.
    """
    script = """
      import { shotInspectorReadiness } from './src/music_video_producer/web/assets/api.js';
      const report = {
        blocking: [{ shot_ids: ['shot_b'], labels: ['SHOT 02 (shot_b)'], reason: 'This shot has no prompt.' }],
        warnings: [{
          shot_ids: ['shot_a', 'shot_c'],
          labels: ['SHOT 01 (shot_a)', 'SHOT 03 (shot_c)'],
          reason: 'These shots carry the same prompt.',
        }],
      };
      console.log(JSON.stringify({
        blocked: shotInspectorReadiness(report, { id: 'shot_b', prompt: '  ' }),
        placeholder: shotInspectorReadiness(report, { id: 'shot_e', prompt: 'New shot' }),
        paired: shotInspectorReadiness(report, { id: 'shot_a', prompt: 'A singer' }),
        otherHalf: shotInspectorReadiness(report, { id: 'shot_c', prompt: 'a  SINGER' }),
        // The report predates the keystroke: a Shot just given a prompt is not blocked any more.
        justWritten: shotInspectorReadiness(report, { id: 'shot_b', prompt: 'Now written' }),
        quiet: shotInspectorReadiness(report, { id: 'shot_d', prompt: 'Unrelated' }),
        noReport: shotInspectorReadiness(null, { id: 'shot_a', prompt: 'A singer' }),
        noShot: shotInspectorReadiness(report, null),
      }));
    """

    notices = run_module(script)
    assert notices["blocked"]["blocked"] is True
    assert notices["blocked"]["flag"] == "NO PROMPT"
    assert "Write a prompt in the shot inspector" in notices["blocked"]["help"]
    # The placeholder is blocked here too, and named as itself rather than as a blank.
    assert notices["placeholder"]["blocked"] is True
    assert notices["placeholder"]["flag"] == "PLACEHOLDER"
    assert "placeholder" in notices["placeholder"]["help"]
    # A Shot in a pair is told which other Shot, under the report's own label for it.
    assert notices["paired"]["blocked"] is False
    assert [line["text"] for line in notices["paired"]["sameness"]] == [
        "Near-duplicate - SHOT 01 (shot_a) and SHOT 03 (shot_c): These shots carry the same prompt."
    ]
    # Both halves of the pair are told, or one of the two Shots is never told anything.
    assert notices["otherHalf"]["sameness"] == notices["paired"]["sameness"]
    # Decided from the prompt on screen, never from a report older than the last keystroke.
    assert notices["justWritten"]["blocked"] is False
    assert notices["justWritten"]["help"] == ""
    for silent in ("quiet", "noReport", "noShot"):
        assert notices[silent]["sameness"] == [], silent
    # A Shot with no prompt is blocked whether or not any report has been fetched.
    assert notices["noShot"]["blocked"] is True

    inspector = without_comments(app_js_block("function renderShotInspector"))
    assert "shotInspectorReadiness(readinessReport, shot)" in inspector
    for rendered in ("readiness.blocked", "readiness.flag", "readiness.help", "readiness.sameness"):
        assert rendered in inspector, rendered
    assert "${readinessHtml}" in inspector
    styles = STYLES_CSS.read_text(encoding="utf-8")
    assert re.search(r"\.shot-readiness \{", styles), "the inspector's blocked state has no style"


def notice_contract() -> dict:
    """api.js's half of the notice contract: what it strips, what it names, and how per kind."""
    return run_module("""
      import { NOTICE_FALLBACK_KIND, NOTICE_JOIN, NOTICE_KINDS, NOTICE_RAW_LABEL, NOTICE_SEPARATOR }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        separator: NOTICE_SEPARATOR,
        join: NOTICE_JOIN,
        kinds: NOTICE_KINDS,
        fallback: NOTICE_FALLBACK_KIND,
        rawLabel: NOTICE_RAW_LABEL,
      }));
    """)


def notice_kind_values() -> set[str] | None:
    """The kinds `MessageNotice.kind` admits, or None while the server carries no discriminator."""
    field = MessageNotice.model_fields.get("kind")
    if field is None:
        return None
    arguments = get_args(field.annotation)
    if arguments:
        return {str(argument) for argument in arguments}
    members = getattr(field.annotation, "__members__", None)
    if members:
        return {str(member.value) for member in members.values()}
    return None


def server_reply_with_notices(tmp_path: Path) -> tuple[dict, str]:
    """One assistant reply exactly as the browser receives it: built by the route, read off it.

    The double is written to produce the two notices that matter together — a document rejection
    carrying raw model output, and the prose-claims-Shots mismatch — under prose that itself
    contains the `\\n\\n---\\n` separator. That last detail is the point: a renderer that split the
    message on `---` would cut this reply in the wrong place, and no hand-written fixture would
    catch it because the server is what decides where the real separator goes.
    """
    from fastapi.testclient import TestClient

    from music_video_producer.app import create_app
    from music_video_producer.config import Settings
    from music_video_producer.store import ProjectStore

    class NoticeDirector:
        """Rejected style bible, echoed treatment, prose claiming shots it does not return."""

        message = (
            "Beat one, the corridor.\n\n---\n"
            "Beat two, the stage. I have written four shots against the second verse."
        )
        style_bible = '[{"style":"<script>alert(1)</script>","palette":["amber","teal"]}]'

        async def plan(self, message, project_context):
            return type(
                "DirectorResult",
                (),
                {
                    "message": self.message,
                    "treatment": project_context["treatment"],
                    "style_bible": self.style_bible,
                    "shots": [],
                },
            )()

    store = ProjectStore(tmp_path)
    project = store.create(Project(name="Notices"))
    project.treatment = "The original treatment, written by hand over several sessions."
    project.style_bible = "Sodium amber, hard backlight, 35mm grain, wardrobe continuity notes."
    store.save(project)
    app = create_app(
        settings=Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy"),
        store=store,
        director=NoticeDirector(),
    )
    response = TestClient(app).post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Break it into beats", "apply_documents": True},
    )

    assert response.status_code == 200, response.text
    reply = response.json()["messages"][-1]
    assert reply["role"] == "assistant"
    # The model's own sentence is returned beside the reply: it is what the prose half of the
    # split has to come back as, and reconstructing it from `content` would be the very guess
    # this test exists to refuse.
    return reply, NoticeDirector.message


def test_the_notice_splitter_and_renderer_are_executed_against_a_real_server_reply(tmp_path: Path):
    """The split and the markup, run over a reply the route really produced.

    Nothing tied the thread render to anything before this: the whole thread was one `innerHTML`
    map that escaped `content`, no test touched it, and a protective refusal was therefore plain
    text after a `---` that read exactly like something the Director said. Asserting a substring
    of app.js would prove the source contains a string, not that a notice renders — so both the
    splitter and the markup it feeds are executed here, and every claim below is read off the
    HTML that would reach the browser.
    """
    from music_video_producer.app import (
        DOCUMENT_REJECTED_NOTICE,
        NOTICE_JOIN,
        NOTICE_SEPARATOR,
        SHOT_CLAIM_WITHOUT_ANY_SHOTS_NOTICE,
    )

    reply, prose = server_reply_with_notices(tmp_path)
    contract = notice_contract()
    # One separator, one join, one spelling — the tail the client strips is the tail the server
    # wrote, and a drift would print every notice twice rather than fail quietly.
    assert contract["separator"] == NOTICE_SEPARATOR
    assert contract["join"] == NOTICE_JOIN

    parsed = run_module(f"""
      import {{ messageBodyHtml, messageParts }}
        from './src/music_video_producer/web/assets/api.js';
      const reply = {json.dumps(reply)};
      console.log(JSON.stringify({{
        parts: messageParts(reply),
        html: messageBodyHtml(reply),
        // The two shapes that must render exactly as they did before notices existed: an
        // ordinary reply, and a message from a manifest saved before the field was added.
        plain: messageBodyHtml({{ role: 'assistant', content: 'Plain <b>reply</b>', notices: [] }}),
        legacy: messageBodyHtml({{ role: 'assistant', content: 'A reply from before.' }}),
      }}));
    """)

    # The prose is the model's own message, whole and unaltered -- including the `---` it wrote
    # itself, which is the sequence a splitter that guessed would have cut it at.
    server_notices = reply["notices"]
    assert len(server_notices) == 2, server_notices
    assert parsed["parts"]["prose"] == prose
    assert parsed["parts"]["prose"].count("---") == 1
    # And the shortcut really would have been wrong on this reply, rather than merely being
    # forbidden: splitting the stored message on the separator loses most of the Director's own
    # sentence, because the model wrote that separator itself.
    assert reply["content"].split(NOTICE_SEPARATOR)[0] != prose
    for notice in server_notices:
        assert notice["text"] not in parsed["parts"]["prose"], notice["text"]
    # Both notices come through as data, in the server's order and the server's words — and under
    # the server's own kind, or the fallback for a manifest written before kinds existed.
    assert parsed["parts"]["notices"] == [
        {
            "kind": notice.get("kind") or contract["fallback"],
            "text": notice["text"],
            "raw": notice["raw"],
        }
        for notice in server_notices
    ]
    assert server_notices[0]["text"].startswith("Style bible was NOT replaced")
    assert server_notices[0]["text"] == DOCUMENT_REJECTED_NOTICE.format(
        document="Style bible", reason="the model returned JSON instead of prose"
    )
    # The fixture project has no shots, so the mismatch is worded for that case.
    assert server_notices[1]["text"] == SHOT_CLAIM_WITHOUT_ANY_SHOTS_NOTICE

    html = parsed["html"]
    # Each notice in a block of its own, after the prose rather than inside it.
    assert html.count('<div class="message-notice ') == 2
    assert html.index('<div class="message-notice ') > html.index("Beat two, the stage")
    # Labelled in words, not only edged in colour: one label per block, and the word is the one
    # its own kind carries rather than a single label stamped on every notice alike.
    assert html.count('<strong class="notice-label"') == 2
    for notice in server_notices:
        label = contract["kinds"][notice.get("kind") or contract["fallback"]]["label"]
        assert label.strip()
        assert f">{label}</strong>" in html, label
    # The raw model output is reachable, behind a disclosure that starts closed. Asserted on the
    # tag itself: the old ` open` scan read the whole body, so the Director's own prose or the raw
    # model output could satisfy it or break it for reasons that have nothing to do with the tag.
    disclosures = re.findall(r"<details[^>]*>", html)
    assert len(disclosures) == 1, disclosures
    assert not re.search(r"[\s\"']open\b", disclosures[0]), disclosures[0]
    assert 'class="notice-raw"' in disclosures[0], disclosures[0]
    assert f"<summary>{contract['rawLabel']}</summary>" in html
    raw_start = html.index("<details")
    assert html.index("alert(1)") > raw_start
    # Escaped after the split, never before it: this raw output is a script tag.
    assert "<script>" in server_notices[0]["raw"]
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # Said once. A message rendered from `content` *and* from its notices would print every
    # refusal twice, which is how a tail-strip that silently stopped matching would look.
    for notice in server_notices:
        assert html.count(escape_for_html(notice["text"])) == 1, notice["text"]

    # And a reply with no notices renders exactly as it did before: escaped prose, no chrome.
    assert parsed["plain"] == "Plain &lt;b&gt;reply&lt;/b&gt;"
    assert parsed["legacy"] == "A reply from before."


def escape_for_html(value: str) -> str:
    """The five replacements `escapeHtml` makes, for asserting against rendered markup."""
    for character, entity in (
        ("&", "&amp;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ('"', "&quot;"),
        ("'", "&#39;"),
    ):
        value = value.replace(character, entity)
    return value


def test_escape_html_escapes_every_character_that_can_close_a_tag_or_an_attribute():
    """`escapeHtml` had no test of its own, and it did not escape `'`.

    It is a shared public export interpolated into attribute positions all over app.js --
    `value="${…}"`, `title="${…}"`, `data-*` -- so the apostrophe is not cosmetic: a value
    carrying one closes any single-quoted attribute and lets the rest be read as markup. Only `<`
    and `>` were ever exercised, and then only indirectly through the notice renderer, so `"` and
    `&` were unasserted too and could have been dropped in silence.
    """
    escaped = run_module("""
      import { escapeHtml } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        ampersand: escapeHtml('a & b'),
        lessThan: escapeHtml('a < b'),
        greaterThan: escapeHtml('a > b'),
        doubleQuote: escapeHtml('say "no"'),
        singleQuote: escapeHtml("the Director's cut"),
        together: escapeHtml(`<img src='x' onerror="alert(1)" & more>`),
        // The order matters: escaping `&` after the others would double-escape their entities.
        entity: escapeHtml('&lt;'),
        empty: escapeHtml(''),
        absent: escapeHtml(),
        number: escapeHtml(7),
        nullish: escapeHtml(null),
      }));
    """)

    assert escaped["ampersand"] == "a &amp; b"
    assert escaped["lessThan"] == "a &lt; b"
    assert escaped["greaterThan"] == "a &gt; b"
    assert escaped["doubleQuote"] == "say &quot;no&quot;"
    assert escaped["singleQuote"] == "the Director&#39;s cut"
    # Nothing that could close a tag or an attribute survives, in either quoting style.
    for raw in ("<", ">", '"', "'"):
        assert raw not in escaped["together"], raw
    assert escaped["together"] == escape_for_html(
        "<img src='x' onerror=\"alert(1)\" & more>"
    )
    # `&` first, or `&lt;` would come back as `&amp;lt;` and the text would read wrong.
    assert escaped["entity"] == "&amp;lt;"
    assert escaped["empty"] == ""
    assert escaped["absent"] == ""
    assert escaped["number"] == "7"
    assert escaped["nullish"] == "null"


def test_the_thread_body_is_built_by_a_pure_function_the_dom_only_assigns():
    """Swapping `thread.innerHTML` for `thread.textContent` was a green mutation.

    app.js is imported by no test and executed by none, so while the thread's markup was built
    there, both assertions the suite had -- that the render calls `messageBodyHtml` and does not
    escape `content` -- stayed satisfied while every refusal rendered as the literal text
    `<div class="message-notice">…` and the block never appeared at all. Double-escaping the body
    was the same class of silent kill. This codebase hit that three times in Story 2.3, so the
    whole body is a pure function now: what a browser would receive is executed here, and the one
    line that turns it into DOM is what is left to pin.
    """
    rendered = run_module("""
      import { threadHtml } from './src/music_video_producer/web/assets/api.js';
      const thread = threadHtml([
        { id: 'msg_1', role: 'user', content: 'Break it into beats <b>now</b>.' },
        {
          id: 'msg_2', role: 'assistant',
          content: 'Beat one.\\n\\n---\\nStyle bible was NOT replaced.',
          notices: [{
            kind: 'refusal', text: 'Style bible was NOT replaced.',
            raw: '<script>alert(1)</script>',
          }],
        },
      ]);
      console.log(JSON.stringify({
        thread,
        empty: threadHtml([]),
        noMessages: threadHtml(undefined),
        nonArray: threadHtml('nope'),
      }));
    """)

    thread = rendered["thread"]
    # Real markup, not markup-shaped text: every structural tag opens as a tag, and everything
    # that came from the model or the Director is escaped inside it.
    assert '<div class="message user">' in thread
    assert '<div class="message assistant">' in thread
    assert '<div class="message-notice notice-refusal"' in thread
    assert "&lt;div class=&quot;message-notice" not in thread, "the body is escaped a second time"
    assert "&amp;lt;" not in thread, "the body is escaped a second time"
    assert "Break it into beats &lt;b&gt;now&lt;/b&gt;." in thread
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in thread
    assert "<script>" not in thread
    # The role the stylesheet keys off is still stamped on every bubble.
    assert thread.count('<div class="message ') == 2

    # One empty state, whatever "no messages" arrives as.
    assert rendered["empty"] == rendered["noMessages"] == rendered["nonArray"]
    assert '<div class="empty-thread">' in rendered["empty"]
    assert '<div class="message ' not in rendered["empty"]
    assert "<" in rendered["empty"] and "&lt;div" not in rendered["empty"]
    # The markup carries that same empty state for the moment before app.js runs, and it cannot
    # import the constants -- so this is what stops the two copies becoming two wordings, the way
    # every other string the markup and a module both hold is held together here.
    placeholder = re.search(
        r'<div class="empty-thread">.*?</div>', INDEX_HTML.read_text(encoding="utf-8"), re.DOTALL
    )
    assert placeholder, "the thread has no empty state before the first render"
    assert placeholder.group(0) == rendered["empty"], placeholder.group(0)

    # And the DOM layer is one assignment of that string, with no markup and no second escape
    # left in it for the suite to be unable to reach.
    render = without_comments(app_js_block("function renderTreatment"))
    assert "thread.innerHTML = threadHtml(project?.messages);" in render
    assert "textContent" not in render, "the thread body would render as literal markup"
    for leaked in ("<div", "messageBodyHtml", "escapeHtml", "empty-thread", "message-notice"):
        assert leaked not in render, leaked


def test_every_notice_block_is_a_named_note_with_its_own_label_id():
    """The block the story calls unmissable had no accessibility semantics at all.

    Its whole "this is the guard speaking, not the Director" was a coloured left edge and a line
    of 9px small caps -- one of which a screen reader does not convey and the other of which it
    reads as one more run of text inside the reply. It is a named `note` now, and the ids have to
    be unique per block or `aria-labelledby` names the wrong one in a thread of several refusals.
    """
    thread = run_module("""
      import { threadHtml } from './src/music_video_producer/web/assets/api.js';
      const notices = [
        { kind: 'refusal', text: 'Style bible was NOT replaced.' },
        { kind: 'flag', text: 'The reply claims shots it did not carry.' },
      ];
      const body = (parts) => 'Prose.\\n\\n---\\n' + parts.map((n) => n.text).join('\\n\\n');
      console.log(JSON.stringify({ thread: threadHtml([
        { id: 'msg_1', role: 'assistant', content: body(notices), notices },
        { id: 'msg_2', role: 'assistant', content: body(notices), notices },
        // No id at all -- a hand-written or pre-id manifest must still get unique ids.
        { role: 'assistant', content: body(notices), notices },
      ]) }));
    """)["thread"]

    blocks = re.findall(r'<div class="message-notice[^"]*"([^>]*)>', thread)
    assert len(blocks) == 6, blocks
    identifiers = []
    for attributes in blocks:
        assert 'role="note"' in attributes, attributes
        named = re.search(r'aria-labelledby="([^"]+)"', attributes)
        assert named, attributes
        identifiers.append(named.group(1))

    # Unique per block, or the note is named by another block's label.
    assert len(set(identifiers)) == len(identifiers), identifiers
    for identifier in identifiers:
        assert thread.count(f'id="{identifier}"') == 1, identifier
        # And what it names is the kind label, so the note's accessible name says which kind it is.
        named = re.search(
            rf'<strong class="notice-label" id="{re.escape(identifier)}">([^<]+)</strong>', thread
        )
        assert named, identifier
        assert named.group(1).strip(), identifier


def test_every_notice_kind_carries_its_own_words_and_its_own_edge_token():
    """One label for every notice reported good news in amber under the word "Safety notice".

    A reply that successfully replaced a document, or wrote prompts for four shots, was dressed
    exactly like the refusal beside it -- which is the alarm fatigue that makes the refusal
    stop registering. Both signals move together per kind: the label's words and the edge's
    colour, executed here from the rendered HTML and matched against the rule that colours it.
    """
    contract = notice_contract()
    kinds = contract["kinds"]
    assert len(kinds) >= 2, kinds
    assert contract["fallback"] in kinds, contract

    rendered = run_module(f"""
      import {{ messageBodyHtml }} from './src/music_video_producer/web/assets/api.js';
      const reply = (kind) => ({{
        id: 'msg_' + String(kind), role: 'assistant',
        content: 'Prose.\\n\\n---\\nWhat happened.',
        notices: [{{ kind, text: 'What happened.' }}],
      }});
      const out = {{}};
      for (const kind of {json.dumps(list(kinds))}) out[kind] = messageBodyHtml(reply(kind));
      out.unknown = messageBodyHtml(reply('catastrophe'));
      out.absent = messageBodyHtml(reply(undefined));
      console.log(JSON.stringify(out));
    """)

    labels = {}
    for kind, presentation in kinds.items():
        block = rendered[kind]
        assert f'<div class="message-notice {presentation["className"]}"' in block, kind
        assert f'>{presentation["label"]}</strong>' in block, kind
        labels[kind] = presentation["label"]
    # Different words per kind, or the label is not a signal that tells them apart.
    assert len(set(labels.values())) == len(labels), labels
    assert len({presentation["className"] for presentation in kinds.values()}) == len(kinds), kinds

    # An unrecognised or missing kind is presented as the fallback -- the cautious one -- rather
    # than as the quietest available, and never as an unstyled block with no label at all.
    fallback = kinds[contract["fallback"]]
    for guessed in ("unknown", "absent"):
        assert f'<div class="message-notice {fallback["className"]}"' in rendered[guessed], guessed
        assert f'>{fallback["label"]}</strong>' in rendered[guessed], guessed

    # The stylesheet colours exactly those class names, each from its own palette token.
    css = STYLES_CSS.read_text(encoding="utf-8")
    rules = dict(css_rules(css))
    root = re.search(r":root\s*\{(.*?)\n\}", css, re.DOTALL)
    assert root, "styles.css no longer declares its palette on :root"
    tokens = {}
    for kind, presentation in kinds.items():
        edge = rules.get(f".message-notice.{presentation['className']}")
        label = rules.get(f".message-notice.{presentation['className']} .notice-label")
        assert edge, f"nothing colours the {kind} edge"
        assert label, f"nothing colours the {kind} label"
        edge_token = re.search(r"border-left-color:\s*var\((--[\w-]+)\)", edge)
        label_token = re.search(r"color:\s*var\((--[\w-]+)\)", label)
        assert edge_token, edge
        assert label_token, label
        # One colour per kind across both signals, and a token the palette really declares.
        assert edge_token.group(1) == label_token.group(1), kind
        assert f"{edge_token.group(1)}:" in root.group(1), edge_token.group(1)
        tokens[kind] = edge_token.group(1)
    # Distinct per kind, or the edge conveys nothing, and no new colour was invented for any of
    # them: every one is a token that already existed.
    assert len(set(tokens.values())) == len(tokens), tokens
    assert tokens[contract["fallback"]] == "--amber", tokens


def test_the_notice_kinds_the_client_renders_are_the_kinds_the_server_sends():
    """A kind the server sends and the client has never heard of falls back to a refusal label.

    That is the safe direction, and it is still wrong: good news would keep arriving in amber. The
    two halves are one set, so this is what stops a kind being added on one side alone.
    """
    server_kinds = notice_kind_values()
    if server_kinds is None:
        pytest.skip("MessageNotice carries no `kind` discriminator yet")
    assert server_kinds == set(notice_contract()["kinds"]), server_kinds


def test_an_empty_notice_does_not_make_every_other_notice_render_twice():
    """The count used to be taken from the filtered list, and the tail then stopped matching.

    One notice with empty text -- and the server is the only thing that decides there is never
    one -- dropped a `\\n\\n` from the reconstructed tail, so `endsWith` failed, the whole joined
    string was kept as prose, and every remaining refusal printed twice: once inside the prose,
    once as a block. A client guarantee that holds only because of a server constraint is not a
    client guarantee.
    """
    parsed = run_module("""
      import { messageBodyHtml, messageParts } from './src/music_video_producer/web/assets/api.js';
      const notices = [
        { kind: 'refusal', text: 'Style bible was NOT replaced.' },
        { kind: 'refusal', text: '' },
        { kind: 'flag', text: 'The reply claims shots it did not carry.' },
      ];
      // Joined exactly as app.assistant_reply joins it: every notice, empty text included.
      const reply = {
        id: 'msg_1', role: 'assistant', notices,
        content: 'Beat one.' + '\\n\\n---\\n' + notices.map((n) => n.text).join('\\n\\n'),
      };
      console.log(JSON.stringify({ parts: messageParts(reply), html: messageBodyHtml(reply) }));
    """)

    assert parsed["parts"]["prose"] == "Beat one."
    # The empty one is no block: there is no sentence in it to read.
    assert [notice["text"] for notice in parsed["parts"]["notices"]] == [
        "Style bible was NOT replaced.",
        "The reply claims shots it did not carry.",
    ]
    html = parsed["html"]
    assert html.count('<div class="message-notice ') == 2
    for said in ("Style bible was NOT replaced.", "The reply claims shots it did not carry."):
        assert html.count(escape_for_html(said)) == 1, said


def test_only_an_assistant_reply_renders_notice_chrome():
    """A user bubble carrying notices was drawn as a protective refusal.

    Nothing on the server puts notices anywhere but an assistant reply, so a `user` or `system`
    message carrying them is a hand-edited manifest or the Director's own words fed back -- and a
    bubble the Director typed made to look like the guard speaking is the exact confusion the
    block exists to end. Nothing is swallowed either: the content is kept whole and escaped.
    """
    rendered = run_module("""
      import { messageBodyHtml } from './src/music_video_producer/web/assets/api.js';
      const notices = [{ kind: 'refusal', text: 'Style bible was NOT replaced.' }];
      const content = 'Do it my way.\\n\\n---\\nStyle bible was NOT replaced.';
      const out = {};
      for (const role of ['user', 'system', 'assistant', 'director', undefined]) {
        out[String(role)] = messageBodyHtml({ id: 'msg_1', role, content, notices });
      }
      console.log(JSON.stringify(out));
    """)

    for impostor in ("user", "system", "director", "undefined"):
        html = rendered[impostor]
        assert "message-notice" not in html, impostor
        assert "notice-label" not in html, impostor
        # Kept whole rather than half-swallowed: the tail is only stripped where it was written.
        assert html == escape_for_html(
            "Do it my way.\n\n---\nStyle bible was NOT replaced."
        ), impostor
    assert '<div class="message-notice ' in rendered["assistant"]
    assert rendered["assistant"].startswith(escape_for_html("Do it my way."))


def test_a_blank_raw_output_offers_evidence_only_when_there_is_some():
    """Whitespace is truthy, so a raw of `"   "` opened a disclosure onto an empty box.

    Offering the evidence behind a refusal and then showing none is worse than not offering it.
    """
    rendered = run_module("""
      import { messageBodyHtml, messageParts } from './src/music_video_producer/web/assets/api.js';
      const reply = (raw) => ({
        id: 'msg_1', role: 'assistant',
        content: 'Prose.\\n\\n---\\nStyle bible was NOT replaced.',
        notices: [{ kind: 'refusal', text: 'Style bible was NOT replaced.', raw }],
      });
      console.log(JSON.stringify({
        blank: messageBodyHtml(reply('   \\n\\t ')),
        emptyRaw: messageBodyHtml(reply('')),
        missing: messageBodyHtml(reply(undefined)),
        nonString: messageBodyHtml(reply(42)),
        real: messageBodyHtml(reply('{"style": "json"}')),
        // Real output that merely begins and ends with whitespace keeps every character of it.
        padded: messageParts(reply('\\n  {"style": "json"}  \\n')).notices[0].raw,
      }));
    """)

    for absent in ("blank", "emptyRaw", "missing", "nonString"):
        assert "<details" not in rendered[absent], absent
        assert "notice-raw" not in rendered[absent], absent
        # The refusal itself is still a block; only the empty promise is gone.
        assert '<div class="message-notice ' in rendered[absent], absent
    assert "<details" in rendered["real"]
    assert escape_for_html('{"style": "json"}') in rendered["real"]
    # Trimming decides presence only -- what is shown is the output exactly as it arrived.
    assert rendered["padded"] == '\n  {"style": "json"}  \n'


def test_a_tail_that_does_not_match_keeps_every_word_of_the_content():
    """The documented fallback branch was never executed, only its matching twin.

    A message whose content the client cannot account for keeps all of it and still shows every
    notice as its own block: printing a refusal twice is a visible defect, and silently swallowing
    half of one is not. Only the branch that strips was covered, so the branch that deliberately
    does not could have been deleted or inverted with the suite green.
    """
    parsed = run_module("""
      import { messageParts } from './src/music_video_producer/web/assets/api.js';
      const notices = [{ kind: 'refusal', text: 'Style bible was NOT replaced.' }];
      const tail = '\\n\\n---\\n' + notices[0].text;
      console.log(JSON.stringify({
        matching: messageParts({ role: 'assistant', notices, content: 'Beat one.' + tail }),
        // Hand-edited, re-wrapped, or written by an older server: the tail is not where it should
        // be, so nothing may be cut off the end on the strength of a guess.
        reworded: messageParts({
          role: 'assistant', notices,
          content: 'Beat one.\\n\\n---\\nStyle bible was not replaced.',
        }),
        trailing: messageParts({ role: 'assistant', notices, content: 'Beat one.' + tail + '\\n' }),
        inTheMiddle: messageParts({
          role: 'assistant', notices, content: 'Beat one.' + tail + '\\n\\nBeat two.',
        }),
        noTailAtAll: messageParts({ role: 'assistant', notices, content: 'Beat one.' }),
        empty: messageParts({ role: 'assistant', notices, content: '' }),
      }));
    """)

    assert parsed["matching"]["prose"] == "Beat one."
    unmatched = {
        "reworded": "Beat one.\n\n---\nStyle bible was not replaced.",
        "trailing": "Beat one.\n\n---\nStyle bible was NOT replaced.\n",
        "inTheMiddle": "Beat one.\n\n---\nStyle bible was NOT replaced.\n\nBeat two.",
        "noTailAtAll": "Beat one.",
        "empty": "",
    }
    for label, content in unmatched.items():
        # Not one character less than arrived, and the notice is still its own block.
        assert parsed[label]["prose"] == content, label
        assert len(parsed[label]["notices"]) == 1, label


def test_the_notice_block_is_edged_named_and_not_reclaimed_by_a_later_rule():
    """A notice that reads like Director prose is the defect; colour alone is not the fix.

    `.message.system` is the precedent — full width, coloured left edge, palette tokens only —
    and this block carries the same constraints plus one that precedent did not have: it nests
    *inside* a `.message`, so the rules it competes with are the bare-element ones the stylesheet
    sets globally, and nothing later may reclaim its edge.
    """
    css = STYLES_CSS.read_text(encoding="utf-8")
    rules = css_rules(css)
    blocks = [
        (index, selector, body)
        for index, (selector, body) in enumerate(rules)
        if "message-notice" in selector
    ]
    assert blocks, "the notice block has no style of its own"
    base = next(item for item in blocks if item[1].strip() == ".message-notice")
    # The bubbles it is told apart from are still styled, or "distinct" means nothing.
    for bubble in (r"\.message\.assistant", r"\.message\.system"):
        assert re.search(bubble + r"\s*\{", css), bubble

    # Nothing declared later takes the edge back -- the `.shot-clip.no-prompt` lesson, where all
    # that was pinned was that a declaration appeared *somewhere* in the rule. The per-kind rules
    # recolour the edge and may not restate the shorthand, which is the only way to lose it.
    shorthands = [index for index, _, body in blocks if re.search(r"border-left:", body)]
    assert shorthands == [base[0]], shorthands
    for index, selector, body in blocks:
        if "border-left-color" not in body:
            continue
        assert index > base[0], selector
        assert selector_specificity(selector) > selector_specificity(".message-notice"), selector
    # The notice's own text rules outrank the bare-element ones that would otherwise set them:
    # `h1, h2, p` sets a paragraph margin globally, and the notice's paragraphs are inside a
    # bubble whose `white-space` and line height are its own.
    assert any(re.search(r"(^|,)\s*p\s*(,|$)", selector.strip()) for selector, _ in rules)
    assert selector_specificity(".message-notice p") > selector_specificity("p")
    assert any(item[1].strip() == ".message-notice p" for item in blocks), blocks

    # The class the disclosure actually carries is the class it is styled through: a class no rule
    # and no test names is a class that outlives whatever it was for.
    raw_rules = [item for item in blocks if ".notice-raw" in item[1]]
    assert raw_rules, "`<details class=\"notice-raw\">` names a class nothing styles"
    assert any("max-height" in body for _, _, body in raw_rules), raw_rules

    # The label is the signal that has to survive the colour being ignored, so it is not 9px.
    label = next(item for item in blocks if item[1].strip() == ".message-notice .notice-label")
    size = re.search(r"font:[^;]*?(\d+)px", label[2])
    assert size and int(size.group(1)) >= 11, label[2]
    assert "uppercase" in label[2], label[2]

    # The existing caution token, reused rather than reinvented, and every colour is a token.
    assert "var(--amber)" in base[2]
    assert not re.search(r"#[0-9a-fA-F]{3,8}", base[2]), base[2]
    root = re.search(r":root\s*\{(.*?)\n\}", css, re.DOTALL)
    assert root, "styles.css no longer declares its palette on :root"
    for index, _, body in blocks:
        assert not re.search(r"#[0-9a-fA-F]{3,8}", body), body
        for token in re.findall(r"var\((--[\w-]+)\)", body):
            assert f"{token}:" in root.group(1), f"{token} is not a palette token"
    # And every one of them was already declared: no new colour was added for this.
    for token in ("--amber:", "--acid:", "--cyan:"):
        assert token in root.group(1), token


# --------------------------------------------------------------------------------------
# The VRAM eject control
#
# Every guarantee about it that lives in the browser is asserted by *running* the deciding
# code, not by reading it: the workspace is booted against the stub DOM with a canned reply
# from `/api/vram-eject` and the control is read afterwards. Three UI guarantees in this
# project have already been found able to invert while a substring assertion stayed green.
# --------------------------------------------------------------------------------------

VRAM_EJECT_STATE = "/api/vram-eject"


def eject_status(**overrides) -> dict:
    """One server answer about the eject, shaped exactly as `vram_eject_state` builds it."""
    return {
        "enabled": True,
        "source": "default",
        "environment_pinned": False,
        "last": None,
        **overrides,
    }


def booted_eject_control(status, project_paths: dict | None = None, body: str = "") -> dict:
    """Boot the workspace against `status` and report what the control ended up showing."""
    responses = {VRAM_EJECT_STATE: {"body": status}, "/api/projects": {"body": []}}
    responses.update(project_paths or {})
    return run_workspace(
        body
        + """
      await flush();
      console.log(JSON.stringify({
        checked: at('#vram-eject').checked,
        disabled: at('#vram-eject').disabled,
        note: at('#vram-eject-note').textContent,
        title: at('#vram-eject-note').title,
        requests: requests.map((item) => ({ path: item.path, method: item.method, body: item.body })),
      }));
    """,
        responses=responses,
    )


def vram_eject_exports() -> dict:
    return run_module("""
      import { VRAM_EJECT_CONTROL, VRAM_EJECT_LABEL, VRAM_EJECT_LAST, VRAM_EJECT_NOTE, VRAM_EJECT_SOURCES }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        control: VRAM_EJECT_CONTROL,
        label: VRAM_EJECT_LABEL,
        note: VRAM_EJECT_NOTE,
        last: VRAM_EJECT_LAST,
        sources: VRAM_EJECT_SOURCES,
      }));
    """)


def test_the_eject_control_exists_in_the_markup_and_ships_showing_nothing():
    """The server owns the value, so the box must not be drawn from the markup's own guess."""
    markup = INDEX_HTML.read_text(encoding="utf-8")
    exports = vram_eject_exports()

    control = re.search(r'<label class="lock-toggle" id="vram-eject-toggle".*?</label>', markup)
    assert control, "the topbar no longer carries the VRAM eject control"
    assert 'id="{}"'.format(exports["control"].removeprefix("#")) in control.group(0)
    assert exports["label"] in control.group(0), exports["label"]
    # Unticked *and* disabled until the server answers. A ticked default here is precisely the
    # "showing a default it is not honouring" failure the environment case is about.
    assert "checked" not in control.group(0), control.group(0)
    assert "disabled" in control.group(0), control.group(0)
    assert 'id="{}"'.format(exports["note"].removeprefix("#")) in markup

    # It is in the system state, beside ComfyUI's own status -- not inside any workspace panel,
    # because it describes the machine rather than the project on screen.
    system_state = re.search(r'<div class="system-state">.*?\n      </div>', markup, re.DOTALL)
    assert system_state and 'id="vram-eject"' in system_state.group(0)


@pytest.mark.parametrize("enabled", [True, False])
def test_the_control_is_painted_from_the_servers_answer_rather_than_a_default(enabled: bool):
    result = booted_eject_control(eject_status(enabled=enabled))
    assert result["checked"] is enabled
    assert result["disabled"] is False


def test_an_environment_disabled_eject_is_shown_as_off_rather_than_as_the_default():
    """The acceptance criterion: the interface must not show a default it is not honouring."""
    result = booted_eject_control(
        eject_status(enabled=False, source="environment", environment_pinned=True)
    )

    assert result["checked"] is False
    assert "Off" in result["note"]
    # And it says *why*, including that the environment will decide again at the next start.
    assert "MVP_LLM_EJECT_BEFORE_RENDER" in result["title"]


def test_a_control_with_no_answer_yet_says_unknown_instead_of_guessing():
    """A failed GET must not report a machine-wide setting as off while renders still eject."""
    result = run_workspace("""
      await flush();
      console.log(JSON.stringify({
        checked: at('#vram-eject').checked,
        disabled: at('#vram-eject').disabled,
        note: at('#vram-eject-note').textContent,
      }));
    """)

    assert result["checked"] is False
    assert result["disabled"] is True
    assert "unknown" in result["note"]


def test_turning_the_control_off_sends_the_boxs_own_value_to_the_one_route():
    result = booted_eject_control(
        eject_status(enabled=True),
        body="""
          const control = at('#vram-eject');
          control.checked = false;
          await fire('#vram-eject:change', { currentTarget: control });
        """,
    )

    put = [item for item in result["requests"] if item["method"] == "PUT"]
    assert put == [
        {"path": "/api/vram-eject", "method": "PUT", "body": '{"enabled":false}'}
    ]


def test_a_refused_change_reverts_the_box_instead_of_leaving_it_lying():
    """A box left ticked after a refusal claims an eject that no render will perform."""
    result = booted_eject_control(
        eject_status(enabled=True),
        body="""
          await flush();
          // The GET has already answered; make the PUT the thing that fails.
          responses.set('/api/vram-eject', { status: 500, body: { detail: 'could not be saved' } });
          const control = at('#vram-eject');
          control.checked = false;
          await fire('#vram-eject:change', { currentTarget: control });
        """,
    )

    assert result["checked"] is True
    assert result["disabled"] is False


def test_a_project_load_refreshes_the_control_and_never_clears_it():
    """The opposite of `apply_documents`: a standing machine setting, not per-turn consent.

    Clearing it on a project load would silently re-enable an eject the Director switched off.
    """
    project = {
        "id": "project_abc",
        "name": "p",
        "shots": [],
        "assets": [],
        "jobs": [],
        "song": None,
        "messages": [],
    }
    result = booted_eject_control(
        eject_status(enabled=False, source="director"),
        project_paths={
            "/api/projects/project_abc": {"body": project},
            "/api/projects/project_abc/readiness": {"body": {"shots": [], "blocking": []}},
        },
        body="""
          await flush();
          requests.length = 0;
          const select = at('#project-select');
          select.value = 'project_abc';
          await fire('#project-select:change', { target: select });
        """,
    )

    assert result["checked"] is False, "a project load reset a machine-level setting"
    assert any(
        item["path"] == "/api/vram-eject" and item["method"] == "GET"
        for item in result["requests"]
    ), "a project load does not refresh what the last eject did"


def test_a_failed_refresh_leaves_the_setting_standing_rather_than_blanking_it():
    """The half of "never cleared" that a working refresh hides.

    Clearing the setting on a project load is only visible when nothing restores it, so the
    refresh is made to fail here. A machine-level setting must not become "unknown", and the
    control must not become unusable, because one incidental GET did not come back.
    """
    project = {
        "id": "project_abc",
        "name": "p",
        "shots": [],
        "assets": [],
        "jobs": [],
        "song": None,
        "messages": [],
    }
    result = booted_eject_control(
        eject_status(enabled=False, source="director"),
        project_paths={
            "/api/projects/project_abc": {"body": project},
            "/api/projects/project_abc/readiness": {"body": {"shots": [], "blocking": []}},
        },
        body="""
          await flush();
          // The startup GET has answered. Take the route away, so the refresh the project load
          // fires cannot come back.
          responses.delete('/api/vram-eject');
          const select = at('#project-select');
          select.value = 'project_abc';
          await fire('#project-select:change', { target: select });
        """,
    )

    assert result["checked"] is False
    assert result["disabled"] is False, "a failed refresh disabled a setting that had not changed"
    assert "unknown" not in result["note"], result["note"]


def test_the_client_reports_every_eject_status_the_server_can_send():
    """A status the client has not learned about must not be dressed up as one it has."""
    from music_video_producer.vram import EjectStatus

    rendered = vram_eject_exports()["last"]
    assert set(rendered) == {status.value for status in EjectStatus}


def test_no_eject_report_presents_a_vram_figure():
    """The half of Story 4.1 that was deliberately dropped, held dropped.

    Measured on 2026-08-18: free VRAM fell 31.6 -> 16.0 GB across one eject of a 4.71 GB
    model, because ComfyUI released its own cache at the same moment. Any figure the client
    printed would attribute ComfyUI's behaviour to the eject.
    """
    exports = vram_eject_exports()
    units = re.compile(r"\d+(\.\d+)?\s*(GB|MB|GiB|MiB|bytes)\b", re.IGNORECASE)
    for sentence in [*exports["last"].values(), *exports["sources"].values()]:
        assert not units.search(sentence), sentence

    # And the sentences the Director reads are built from residency alone: a status carrying a
    # plausible-looking figure in its `detail` never reaches the note.
    note = run_module("""
      import { vramEjectNote } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify(vramEjectNote({
        enabled: true, source: 'default', environment_pinned: false,
        last: { status: 'released', detail: 'freed 15.6 GB of VRAM',
                resident_before: ['qwen3-vl-4b'], resident_after: [] },
      })));
    """)
    assert "15.6" not in note and "GB" not in note, note
    assert "qwen3-vl-4b" in note, note


def test_the_note_reports_which_models_went_rather_than_asserting_a_release():
    """Executed against the shape the route really produces for each interesting outcome."""
    notes = run_module("""
      import { vramEjectNote } from './src/music_video_producer/web/assets/api.js';
      const say = (last) => vramEjectNote({ enabled: true, source: 'default', environment_pinned: false, last });
      console.log(JSON.stringify({
        released: say({ status: 'released', detail: '', resident_before: ['a', 'b'], resident_after: [] }),
        stillResident: say({ status: 'still-resident', detail: '', resident_before: ['a'], resident_after: ['a'] }),
        off: say({ status: 'disabled', detail: '', resident_before: [], resident_after: [] }),
        unknownStatus: say({ status: 'invented-later', detail: '', resident_before: [], resident_after: [] }),
      }));
    """)

    assert "a, b" in notes["released"]
    assert "a" in notes["stillResident"] and "did not go" in notes["stillResident"]
    assert "no eject was attempted" in notes["off"]
    # Never reported as a success, and never silently blank.
    assert "invented-later" in notes["unknownStatus"]
    assert "submitted anyway" in notes["unknownStatus"]
# The count phrase the character prompt used to carry, assembled at runtime. This file is
# inside `test_nothing_that_runs_states_a_number_of_panels`' scan, so writing the old prompt
# out whole would be a count in a test -- the exact thing the scan forbids -- reported here.
MULTIVIEW_COUNT_PHRASE = "four" + "-panel "

# The character prompt exactly as it shipped before objects could be promoted, with the count
# spliced back in. What the workspace sends today must be this string and nothing else, minus
# that phrase: a Director whose sheets came from the old wording must not find the new one
# asking for a different person, a different set of views, or different lighting.
HISTORICAL_CHARACTER_PROMPT = (
    "Preserve the exact identity, facial features, body type and wardrobe of this character. "
    "Convert the character into a clean "
    + MULTIVIEW_COUNT_PHRASE
    + "character sheet showing a face close-up, front full body, side full body and back full "
    "body view. Consistent neutral lighting and proportions across every view."
)


def multiview_plans(kinds: list[str]) -> dict:
    """`multiviewPlan` run for real, once per Asset kind, with and without a rendered image."""
    return run_module(f"""
      import {{ multiviewPlan }} from './src/music_video_producer/web/assets/api.js';
      const kinds = {json.dumps(kinds)};
      const plans = {{}};
      for (const kind of kinds) {{
        plans[kind] = {{
          rendered: multiviewPlan({{ id: 'a', kind, path: 'out/a.png' }}),
          pending: multiviewPlan({{ id: 'a', kind, path: '' }}),
        }};
      }}
      console.log(JSON.stringify({{
        plans,
        nothingSelected: multiviewPlan(null),
        unknownKind: multiviewPlan({{ id: 'a', kind: 'invented_later', path: 'out/a.png' }}),
      }}));
    """)


def test_the_subject_kind_picks_the_template_and_the_route_agrees_about_which_kinds():
    """Executed for every kind an Asset can carry, against the route's own mapping.

    Two silent failures this closes, and neither is visible from one side. A kind the button
    offers and the route refuses spends the Director a click to reach a 422. A kind the route
    would accept and the button never offers is a feature that does not exist on screen while
    every backend test passes. So the frontend's table is *run* here and compared against
    `app.py`'s, rather than both being asserted separately against a list written twice.

    The templates themselves are the other half. An object getting the character sentence
    would still promote, still produce a sheet, and still pass every status-code test in the
    suite -- while asking a cargo hauler to preserve its facial features and wardrobe.
    """
    kinds = list(get_args(AssetKind))
    executed = multiview_plans(kinds)
    plans = executed["plans"]

    promotable = {kind for kind in kinds if plans[kind]["rendered"] is not None}
    assert promotable == set(MULTIVIEW_SUBJECTS), "the button and the route disagree"

    prompts = {kind: plans[kind]["rendered"]["prompt"] for kind in promotable}
    # The character path means what it meant: the old sentence, less the count it asserted.
    assert prompts["character"] == HISTORICAL_CHARACTER_PROMPT.replace(MULTIVIEW_COUNT_PHRASE, "")
    # An object gets its own template, not the character one reworded to cover both.
    assert prompts["prop"] != prompts["character"]
    # Both object kinds share it -- one template for objects, keyed by what the sheet is of.
    assert prompts["prop"] == prompts["setting"]
    for absent in ("identity", "facial", "wardrobe", "character"):
        assert absent not in prompts["prop"].lower(), absent
    for present in ("silhouette", "proportions", "materials", "markings"):
        assert present in prompts["prop"].lower(), present

    # No template states how many of anything the sheet will come back with, executed against
    # what is actually emitted rather than against the source the constants are written in.
    for kind, prompt in prompts.items():
        assert not PANEL_COUNT_PATTERN.search(prompt), (kind, prompt)

    # A pending render is offered but not ready; nothing selected and an unknown kind are
    # neither, so the inspector has nothing to draw rather than a button that 422s.
    assert plans["character"]["rendered"]["ready"] is True
    assert plans["character"]["pending"]["ready"] is False
    assert executed["nothingSelected"] is None
    assert executed["unknownKind"] is None


def test_the_inspector_offers_promotion_for_an_object_and_sends_the_object_template():
    """The render and the click are both executed; nothing here reads app.js as text.

    `multiviewPlan` being correct is not the same as the workspace using it. The inspector
    could go on testing `asset.kind === "character"` in its own template string and the click
    could go on holding a hardcoded sentence, with every assertion in the test above still
    passing and no object promotable from the screen at all. So the inspector is rendered for
    each kind and its markup read back, and then the button is actually clicked and the
    request it produced inspected -- which is the only thing that proves *which* template
    leaves the browser.
    """
    fired = run_workspace("""
      const arrange = (kind, path) => {
        state.project = { id: 'p1', shots: [], jobs: [], assets: [{ id: 'a1', kind, path, name: 'Cargo hauler', source: 'flux-image-gen', created_at: '2026-08-18T00:00:00Z' }] };
        state.selectedAssetId = 'a1';
        app.renderAssetInspector();
        return at('#asset-inspector').innerHTML;
      };
      const offered = {};
      for (const kind of ['character', 'prop', 'setting', 'style', 'image', 'audio', 'video']) {
        offered[kind] = arrange(kind, 'out/a.png').includes('id="create-multiview"');
      }
      const pending = arrange('prop', '');
      const send = async (kind) => {
        arrange(kind, 'out/a.png');
        requests.length = 0;
        await fire('#create-multiview:click', {});
        return requests.map((sent) => ({ path: sent.path, method: sent.method, body: sent.body }));
      };
      const ship = await send('prop');
      const person = await send('character');
      arrange('audio', 'out/a.wav');
      requests.length = 0;
      await fire('#create-multiview:click', {});
      const unsupported = [...requests];
      console.log(JSON.stringify({
        shown: offered,
        pendingShown: pending.includes('id="create-multiview"'),
        pendingDisabled: pending.includes('id="create-multiview" disabled'),
        ship, person, unsupported,
      }));
    """)

    assert fired["shown"] == {
        "character": True, "prop": True, "setting": True,
        "style": False, "image": False, "audio": False, "video": False,
    }
    # A prop with no render yet is offered and shut, not hidden: it says "once this exists".
    assert fired["pendingShown"] is True
    assert fired["pendingDisabled"] is True

    ship = fired["ship"][0]
    assert ship["path"] == "/api/projects/p1/assets/a1/multiview"
    assert ship["method"] == "POST"
    ship_prompt = json.loads(ship["body"])["prompt"]
    assert "silhouette" in ship_prompt and "wardrobe" not in ship_prompt

    person_prompt = json.loads(fired["person"][0]["body"])["prompt"]
    assert person_prompt == HISTORICAL_CHARACTER_PROMPT.replace(MULTIVIEW_COUNT_PHRASE, "")
    assert person_prompt != ship_prompt

    # The click on a kind the feature does not cover sends nothing at all, so a stale
    # selection cannot reach the route behind the inspector's back.
    assert fired["unsupported"] == []
# --------------------------------------------------------------------------------------------
# Shot mode and asset roles, across the language boundary.
# --------------------------------------------------------------------------------------------

# Every shot shape the two implementations are compared over. One list, used by all three parity
# tests below, because the value of a cross-language comparison is entirely in what it covers: a
# matrix that omitted the disagreement cases would prove only that two copies of the easy path
# agree. Each entry names the state it exists to pin.
SHOT_PARITY_CASES = {
    # The two shapes that exist in every project saved before this change.
    "legacy_references": {"asset_ids": ["asset_a", "asset_b"], "use_song_audio": False},
    "legacy_text": {"asset_ids": [], "use_song_audio": False},
    "legacy_song_only": {"asset_ids": [], "use_song_audio": True},
    # A legacy `mode` string, which is a dropdown position and not a declaration.
    "legacy_mode_string": {"mode": "text", "asset_ids": ["asset_a"]},
    # Declarations, fitting and not.
    "declared_text": {"mode": "text_to_video"},
    "declared_text_with_assets": {"mode": "text_to_video", "asset_ids": ["asset_a"]},
    "declared_text_with_song": {"mode": "text_to_video", "use_song_audio": True},
    "declared_references": {"mode": "references", "asset_ids": ["asset_a"], "use_song_audio": True},
    "first_last_complete": {
        "mode": "first_last",
        "citations": [
            {"asset_id": "asset_a", "role": "first", "order": 0},
            {"asset_id": "asset_b", "role": "last", "order": 0},
        ],
    },
    "first_last_missing_one": {
        "mode": "first_last",
        "citations": [{"asset_id": "asset_a", "role": "first", "order": 0}],
    },
    "first_middle_last_missing_middle": {
        "mode": "first_middle_last",
        "citations": [
            {"asset_id": "asset_a", "role": "first", "order": 0},
            {"asset_id": "asset_b", "role": "last", "order": 0},
        ],
    },
    "extend_without_source": {"mode": "extend"},
    # Keyframes riding the references mode: fitting, and over the one-per-role ceiling. Both
    # sides must agree that the first shape is now clean and the second is refused — the exact
    # rows the role-gate change moved.
    "references_with_keyframes": {
        "mode": "references",
        "citations": [
            {"asset_id": "asset_a", "role": "reference", "order": 0},
            {"asset_id": "asset_b", "role": "first", "order": 0},
            {"asset_id": "asset_c", "role": "last", "order": 0},
        ],
    },
    "references_two_first_frames": {
        "mode": "references",
        "citations": [
            {"asset_id": "asset_a", "role": "first", "order": 0},
            {"asset_id": "asset_b", "role": "first", "order": 1},
        ],
    },
    "image_to_video_with_two": {
        "mode": "image_to_video",
        "citations": [
            {"asset_id": "asset_a", "role": "first", "order": 0},
            {"asset_id": "asset_b", "role": "first", "order": 1},
        ],
    },
    # Ordering within a role, with an explicit order that contradicts list position.
    "ordered_references": {
        "citations": [
            {"asset_id": "asset_c", "role": "reference", "order": 2},
            {"asset_id": "asset_b", "role": "reference", "order": 1},
            {"asset_id": "asset_m", "role": "middle", "order": 0},
        ],
    },
}


def parity_shots() -> list[dict]:
    """The matrix as whole shot objects, in one fixed order both languages iterate."""
    return [
        {
            "id": f"shot_{name}", "start": 0, "duration": 5, "prompt": "A singer turns",
            "mode": None, "asset_ids": [], "citations": [], "reference_labels": {},
            "singing": "unknown", "use_song_audio": False, "seed": 0, "status": "draft",
            "prompt_id": "", "latest_output": "", "approved_output": "", "locked": False,
            **fields,
        }
        for name, fields in SHOT_PARITY_CASES.items()
    ]


def test_the_shot_mode_table_is_the_same_table_in_both_languages():
    """`api.js`'s SHOT_MODES against `models.SHOT_MODE_SPECS`, field for field.

    Two hand-written copies of what a mode requires is how the inspector starts drawing a shot as
    complete that the route then refuses — and the inspector is where a Director decides whether to
    spend a render. The order is compared too, because it is the order the mode select offers and a
    reordering that put an unrenderable mode first would change what a new shot is nudged towards.
    """
    offered = run_module("""
      import { SHOT_MODES, ASSET_ROLE_LABELS, SINGING_STATES, LEGACY_SHOT_MODES }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({ SHOT_MODES, ASSET_ROLE_LABELS, SINGING_STATES, LEGACY_SHOT_MODES }));
    """)

    assert [entry["value"] for entry in offered["SHOT_MODES"]] == list(SHOT_MODE_SPECS)
    for entry in offered["SHOT_MODES"]:
        spec = SHOT_MODE_SPECS[entry["value"]]
        assert entry["label"] == spec.label, entry["value"]
        assert entry["song_audio"] == spec.song_audio, entry["value"]
        assert entry["adapter"] == spec.adapter, entry["value"]
        # The workflow name the mode select prints, held to the server's table so which MiniMax
        # graph a mode employs is decided in exactly one place — and held to the adapter, so a
        # mode can never name a workflow it cannot render through or render through one unnamed.
        assert entry["workflow"] == spec.workflow, entry["value"]
        assert bool(spec.workflow) == bool(spec.adapter), entry["value"]
        assert entry["roles"] == [
            {"role": requirement.role, "minimum": requirement.minimum, "maximum": requirement.maximum}
            for requirement in spec.roles
        ], entry["value"]

    assert offered["ASSET_ROLE_LABELS"] == ASSET_ROLE_LABELS
    assert [entry["value"] for entry in offered["SINGING_STATES"]] == list(get_args(SingingState))
    # `unknown` is first, so a select that has never been touched reads as the honest absence
    # rather than as a claim about the performance.
    assert offered["SINGING_STATES"][0]["value"] == "unknown"
    assert set(offered["LEGACY_SHOT_MODES"]) == set(LEGACY_SHOT_MODES)


def test_both_languages_resolve_the_same_mode_for_the_same_shot():
    """`resolveShotMode` against `resolve_shot_mode`, over every case in the matrix.

    Executed on both sides rather than read, because this is the branch that decides what a render
    *is*. A browser that resolved a shot differently from the server would draw one mode in the
    inspector and submit another, and the only place that disagreement would ever surface is in the
    finished video.
    """
    shots = parity_shots()
    resolved = run_module(f"""
      import {{ resolveShotMode }} from './src/music_video_producer/web/assets/api.js';
      const shots = {json.dumps(shots)};
      console.log(JSON.stringify(shots.map(resolveShotMode)));
    """)

    assert resolved == [resolve_shot_mode(Shot.model_validate(shot)) for shot in shots]
    # The matrix really does exercise more than one answer, or this would pass on a pair of
    # constants that both return "references".
    assert len(set(resolved)) >= 4


def test_both_languages_name_the_same_missing_roles():
    """`shotSpecificationProblems` against `mode_specification_problems`, sentence for sentence.

    Compared as full strings and not as counts: the inspector prints these to the Director, the
    route puts them in its 422, and the whole point is that the two say the same thing. A browser
    reporting a different reason from the one the refusal will give is a Director fixing the wrong
    thing.
    """
    shots = parity_shots()
    problems = run_module(f"""
      import {{ shotSpecificationProblems }} from './src/music_video_producer/web/assets/api.js';
      const shots = {json.dumps(shots)};
      console.log(JSON.stringify(shots.map(shotSpecificationProblems)));
    """)

    expected = [mode_specification_problems(Shot.model_validate(shot)) for shot in shots]
    assert problems == expected
    # And the matrix contains real problems as well as clean shots, so agreement is not agreement
    # about a list that is always empty.
    assert any(entry for entry in expected)
    assert any(not entry for entry in expected)


def test_both_languages_reconcile_citations_and_asset_ids_identically():
    """`reconcileShotCitations` against `Shot`'s own validator.

    The client half exists because the shots write does not adopt its own reply — it re-renders
    from local state — so a browser that only wrote `citations` would go on drawing a stale
    `asset_ids` until the next full project load. That makes this a second implementation of a
    model invariant, which is exactly the kind that drifts.
    """
    shots = parity_shots()
    reconciled = run_module(f"""
      import {{ reconcileShotCitations }} from './src/music_video_producer/web/assets/api.js';
      const shots = {json.dumps(shots)};
      console.log(JSON.stringify(shots.map((shot) => {{
        reconcileShotCitations(shot);
        return {{ asset_ids: shot.asset_ids, citations: shot.citations }};
      }})));
    """)

    for entry, shot in zip(reconciled, shots, strict=True):
        model = Shot.model_validate(shot)
        assert entry["asset_ids"] == model.asset_ids, shot["id"]
        assert [(item["asset_id"], item["role"]) for item in entry["citations"]] == [
            (item.asset_id, item.role) for item in model.citations
        ], shot["id"]


def test_the_inspector_draws_the_mode_it_resolves_and_declares_only_what_was_chosen():
    """The mode control, rendered and used, against the workspace's own code.

    Source reading cannot tell a select that is bound to `shot.mode` from one that is drawn and
    never read, and it cannot tell `""` from `null` on the wire — which is the whole difference
    between "the Director has not declared a mode" and a validation error. So the panel is drawn,
    the select is fired, and what the save actually sent is read back off the request.
    """
    rendered = run_workspace("""
      const project = (fields) => ({
        id: 'p1', jobs: [], song: null,
        assets: [{ id: 'asset_a', name: 'Grey wolf', kind: 'prop', path: 'media/wolf.png' }],
        shots: [{ id: 'shot_a', start: 0, duration: 5, prompt: 'A wolf crosses the clearing',
                  mode: null, asset_ids: [], citations: [], reference_labels: {},
                  singing: 'unknown', use_song_audio: false, seed: 0, status: 'draft',
                  prompt_id: '', latest_output: '', approved_output: '', locked: false, ...fields }],
      });
      const draw = (fields) => {
        state.project = project(fields);
        state.selectedShotId = 'shot_a';
        app.renderShotInspector();
        return at('#shot-inspector').innerHTML;
      };
      const undeclaredText = draw({});
      const undeclaredReferences = draw({ asset_ids: ['asset_a'] });
      const declared = draw({ mode: 'first_middle_last', citations: [{ asset_id: 'asset_a', role: 'first', order: 0 }] });

      // Declare a mode through the select and read what reached the wire.
      draw({});
      at('#shot-mode').value = 'first_last';
      at('#shot-singing').value = 'singing';
      requests.length = 0;
      fire('#shot-mode:change', {});
      // The shots write is a chained promise, so the request is not on the wire until the
      // microtask queue drains. Reading `requests` without this asserts about a save that has not
      // happened yet -- and would pass just as happily against a handler that never saved at all.
      await flush();
      const declaredWrite = JSON.parse(requests[requests.length - 1].body).shots[0];

      // And take the declaration back off again.
      draw({ mode: 'first_last' });
      at('#shot-mode').value = '';
      at('#shot-singing').value = 'unknown';
      requests.length = 0;
      fire('#shot-mode:change', {});
      await flush();
      const undeclaredWrite = JSON.parse(requests[requests.length - 1].body).shots[0];

      console.log(JSON.stringify({
        undeclaredText, undeclaredReferences, declared,
        declaredWrite: { mode: declaredWrite.mode, singing: declaredWrite.singing },
        undeclaredWrite: { mode: undeclaredWrite.mode, singing: undeclaredWrite.singing },
      }));
    """)

    # An undeclared shot says so *and* says what it renders as, because "not declared" on its own
    # tells the Director nothing about what pressing render would do.
    assert "Not declared — renders as Text to video" in rendered["undeclaredText"]
    assert "Not declared — renders as References to video" in rendered["undeclaredReferences"]
    # A declared mode is the selected option, and every mode is offered — including the ones with
    # no adapter, which are labelled rather than hidden. A renderable mode names the workflow it
    # renders through, from the spec table and never hand-typed here, because "Text to video"
    # alone never told the Director the MiniMax H3 Director graph is what that click employs.
    assert '<option value="first_middle_last" selected>' in rendered["declared"]
    for mode, spec in SHOT_MODE_SPECS.items():
        assert f'value="{mode}"' in rendered["declared"], mode
        if spec.adapter:
            assert f"{spec.label} — renders through {spec.workflow}" in rendered["declared"], mode
        else:
            assert f"{spec.label} — planned, not yet renderable" in rendered["declared"], mode

    # `null` and not `""`. The model's field is `ShotMode | None` precisely so that "undeclared" is
    # representable, and `""` is not a member of the Literal.
    assert rendered["declaredWrite"] == {"mode": "first_last", "singing": "singing"}
    assert rendered["undeclaredWrite"] == {"mode": None, "singing": "unknown"}
    assert Shot(start=0, duration=5, **rendered["declaredWrite"]).mode == "first_last"
    assert Shot(start=0, duration=5, **rendered["undeclaredWrite"]).mode is None


def test_the_inspector_shows_a_role_per_citation_and_writes_the_role_that_was_chosen():
    """Roles are editable where the shot is edited, and the write carries them.

    The role is the thing that has to be changeable: the same wolf is a middle frame in this shot
    and a plain reference in the next, and a role stored on the asset would have forced a duplicate
    per part. So the row is drawn per citation, every role is offered on every row, and the change
    is followed all the way onto the wire.
    """
    rendered = run_workspace("""
      state.project = {
        id: 'p1', jobs: [], song: null,
        assets: [
          { id: 'asset_wolf', name: 'Grey wolf', kind: 'prop', path: 'media/wolf.png' },
          { id: 'asset_stage', name: 'Stage', kind: 'setting', path: 'media/stage.png' },
        ],
        shots: [{ id: 'shot_a', start: 0, duration: 5, prompt: 'A wolf crosses the clearing',
                  mode: 'first_last', asset_ids: ['asset_wolf', 'asset_stage'],
                  citations: [
                    { asset_id: 'asset_wolf', role: 'reference', order: 0 },
                    { asset_id: 'asset_stage', role: 'reference', order: 1 },
                  ],
                  reference_labels: {}, singing: 'unknown', use_song_audio: false, seed: 0,
                  status: 'draft', prompt_id: '', latest_output: '', approved_output: '',
                  locked: false }],
      };
      state.selectedShotId = 'shot_a';
      app.renderShotInspector();
      const html = at('#shot-inspector').innerHTML;

      // Re-role the wolf to the first frame through the control the Director would use: the select
      // the panel drew for that citation, found the way the panel's own binding found it.
      at('.citation-role[asset_wolf]').value = 'first';
      requests.length = 0;
      fire('.citation-role[asset_wolf]:change', {});
      await flush();
      const rolled = JSON.parse(requests[requests.length - 1].body).shots[0];

      // And remove the other one, through its own control.
      requests.length = 0;
      fire('.remove-ref[asset_stage]:click', {});
      await flush();
      const removed = JSON.parse(requests[requests.length - 1].body).shots[0];

      console.log(JSON.stringify({
        html,
        rolled: { citations: rolled.citations, asset_ids: rolled.asset_ids },
        removed: { citations: removed.citations, asset_ids: removed.asset_ids },
        rerolled: at('#shot-inspector').innerHTML,
        problems: contract.shotSpecificationProblems(state.project.shots[0]),
      }));
    """)

    # A row per citation, each with its own role select bound to that asset.
    assert rendered["html"].count('class="citation-role"') == 2
    assert 'data-id="asset_wolf"' in rendered["html"]
    assert 'data-id="asset_stage"' in rendered["html"]
    # Every role is offered, not only the ones this mode declares: a Director re-pointing a shot
    # does it one control at a time, and hiding `middle` until the mode was already right would
    # make the order of those two clicks matter.
    for role, label in ASSET_ROLE_LABELS.items():
        assert f'<option value="{role}"' in rendered["html"], role
        assert label in rendered["html"], role

    # The role really reached the wire, and the reference projection no longer claims the wolf —
    # which is what stops the render sending it as reference picture one under a mode that says it
    # is the first frame.
    assert [(item["asset_id"], item["role"]) for item in rendered["rolled"]["citations"]] == [
        ("asset_wolf", "first"), ("asset_stage", "reference")
    ]
    assert rendered["rolled"]["asset_ids"] == ["asset_stage"]
    # And what the server would make of that body is the same thing, rather than the client's own
    # idea of it.
    saved = Shot.model_validate({"start": 0, "duration": 5, **rendered["rolled"]})
    assert saved.asset_ids == ["asset_stage"]

    # Removing a citation removes exactly that one, in both fields.
    assert [item["asset_id"] for item in rendered["removed"]["citations"]] == ["asset_wolf"]
    assert rendered["removed"]["asset_ids"] == []

    # And the panel reports exactly what the route would refuse the shot for.
    assert rendered["problems"] == mode_specification_problems(
        Shot(
            start=0, duration=5, mode="first_last",
            citations=[AssetCitation(asset_id="asset_wolf", role="first")],
        )
    )
    assert rendered["problems"]


def test_a_citation_whose_asset_is_gone_is_drawn_rather_than_skipped():
    """The deleted-asset row, proven by executing the panel that used to swallow it.

    The attached list used to return `""` for a citation whose asset was missing, so a shot could
    look like it had dropped an attachment it was in fact still sending — and the route refuses
    that shot with `Unknown reference asset`, a refusal the Director had no way to see coming.
    """
    rendered = run_workspace("""
      state.project = {
        id: 'p1', jobs: [], song: null,
        assets: [{ id: 'asset_stage', name: 'Stage', kind: 'setting', path: 'media/stage.png' }],
        shots: [{ id: 'shot_a', start: 0, duration: 5, prompt: 'A wolf crosses the clearing',
                  mode: null, asset_ids: ['asset_gone', 'asset_stage'],
                  citations: [], reference_labels: {}, singing: 'unknown', use_song_audio: false,
                  seed: 0, status: 'draft', prompt_id: '', latest_output: '', approved_output: '',
                  locked: false }],
      };
      state.selectedShotId = 'shot_a';
      app.renderShotInspector();
      console.log(JSON.stringify({
        html: at('#shot-inspector').innerHTML,
        dangling: contract.danglingCitations(state.project, state.project.shots[0]),
        label: contract.CITATION_MISSING_LABEL,
      }));
    """)

    assert rendered["dangling"] == ["asset_gone"]
    assert rendered["html"].count('class="citation-row') == 2
    # Named in words as well as flagged by the row's class, for the reason the readiness flag
    # carries both: colour alone is state by appearance.
    assert rendered["label"] in rendered["html"]
    assert "citation-missing" in rendered["html"]
    assert "asset_gone" in rendered["html"]
    # The surviving asset is still drawn normally beside it, so the panel is not simply broken.
    assert "Stage" in rendered["html"]

    # And the same fact from the server's own function, so the two halves agree about which
    # citations are dangling rather than each having its own idea.
    project = Project(name="Gone")
    project.assets = [Asset(id="asset_stage", name="Stage", kind="setting", path="media/stage.png")]
    assert dangling_citations(
        project, Shot(start=0, duration=5, asset_ids=["asset_gone", "asset_stage"])
    ) == rendered["dangling"]


# ---------------------------------------------------------------------------------------------
# The H3 expansion controls: the shot inspector's "Expand prompt", and the plan-wide sweep
# ---------------------------------------------------------------------------------------------

H3_EXPANSION = (
    "integrated_multimodal_description: [Shot 1] A grey wolf crosses the clearing.\n"
    "overall_soundscape: Dry needles compress underfoot.\n"
    "non_diegetic_music: A low cello figure, slow."
)


def expansion_shot(**fields) -> str:
    """One shot literal for the workspace harness, in the shape a project reply carries."""
    base = {
        "id": "shot_a", "start": 0, "duration": 5, "prompt": "A wolf crosses the clearing",
        "h3_prompt": "", "mode": None, "asset_ids": [], "citations": [], "reference_labels": {},
        "singing": "unknown", "use_song_audio": False, "seed": 0, "status": "draft",
        "prompt_id": "", "latest_output": "", "approved_output": "", "locked": False,
    }
    return json.dumps({**base, **fields})


def test_the_expand_prompt_control_decides_every_state_from_the_servers_own_rules():
    """Executed for every state, because the states are the feature.

    The refusal *order* is the load-bearing part and is phase one's: `shot_write_refusal` before
    the prompt gate, so a locked shot with no intent hears that it is locked rather than being sent
    to write an intent that would then be refused anyway.
    """
    decided = run_module("""
      import { expandPromptControl } from './src/music_video_producer/web/assets/api.js';
      const shot = (fields) => ({ id: 's', prompt: 'A wolf crosses the clearing', h3_prompt: '',
                                  status: 'draft', locked: false, prompt_id: '',
                                  latest_output: '', approved_output: '', ...fields });
      console.log(JSON.stringify({
        none: expandPromptControl(null),
        open: expandPromptControl(shot({})),
        expanded: expandPromptControl(shot({ h3_prompt: 'integrated_multimodal_description: x' })),
        locked: expandPromptControl(shot({ locked: true })),
        rendered: expandPromptControl(shot({ status: 'complete', prompt_id: 'abc' })),
        blank: expandPromptControl(shot({ prompt: '' })),
        placeholder: expandPromptControl(shot({ prompt: 'New shot' })),
        lockedAndBlank: expandPromptControl(shot({ locked: true, prompt: '' })),
      }));
    """)

    assert decided["none"]["shown"] is False
    assert decided["open"] == {
        "shown": True, "disabled": False, "label": "Expand prompt",
        "title": decided["open"]["title"], "reason": "",
    }
    # A shot that already has one says so before the click: the matrix's re-expansion row is a
    # replacement, and "Expand prompt" over an expanded shot reads as an offer to add a second.
    assert decided["expanded"]["label"] == "Expand prompt again"
    assert decided["expanded"]["disabled"] is False
    # Every refusal is drawn as a shut control carrying its reason, never as no control at all.
    for state in ("locked", "rendered", "blank", "placeholder", "lockedAndBlank"):
        assert decided[state]["shown"] is True, state
        assert decided[state]["disabled"] is True, state
        assert decided[state]["reason"], state
        assert decided[state]["title"] == decided[state]["reason"], state
    assert "locked" in decided["locked"]["reason"].lower()
    assert "already rendered" in decided["rendered"]["reason"].lower()
    assert "creative intent" in decided["blank"]["reason"].lower()
    assert decided["placeholder"]["reason"] == decided["blank"]["reason"]
    # Phase one's order, one screen earlier.
    assert decided["lockedAndBlank"]["reason"] == decided["locked"]["reason"]


def test_the_inspector_draws_the_expand_control_under_the_intent_and_the_expansion_beside_it():
    """Drawn where the Director edits the text, which is what the Director asked for in their own
    words. Executed rather than read, because a control that renders and one that is bound to a
    handler are two different claims and only one of them is greppable."""
    rendered = run_workspace(f"""
      const draw = (shot) => {{
        state.project = {{ id: 'p1', jobs: [], song: null, assets: [], shots: [shot] }};
        state.selectedShotId = 'shot_a';
        app.renderShotInspector();
        return at('#shot-inspector').innerHTML;
      }};
      console.log(JSON.stringify({{
        open: draw({expansion_shot()}),
        expanded: draw({expansion_shot(h3_prompt=H3_EXPANSION)}),
        locked: draw({expansion_shot(locked=True)}),
        blank: draw({expansion_shot(prompt="")}),
      }}));
    """)

    # The control is present in every state, and sits between the creative intent and the seed --
    # in the text section, under the thing it expands.
    for state, markup in rendered.items():
        assert 'id="expand-prompt"' in markup, state
        assert markup.index("shot-prompt") < markup.index('id="expand-prompt"'), state
        assert markup.index('id="expand-prompt"') < markup.index("shot-seed"), state

    # The expansion box is drawn only for a shot that has one, so its presence is the panel's
    # answer to "is this shot expanded".
    assert 'id="shot-h3-prompt"' not in rendered["open"]
    assert 'id="shot-h3-prompt"' in rendered["expanded"]
    assert "A grey wolf crosses the clearing." in rendered["expanded"]
    # ...and it is editable, not a read-only display: the frozen block says both fields are
    # independently editable.
    box = re.search(r'<textarea id="shot-h3-prompt"[^>]*>', rendered["expanded"])
    assert box and "readonly" not in box.group(0), rendered["expanded"]
    assert "Expand prompt again" in rendered["expanded"]

    # A refused control is drawn shut, with its reason on screen rather than only in hover text.
    for state in ("locked", "blank"):
        button = re.search(r'<button[^>]*id="expand-prompt"[^>]*>', rendered[state])
        assert button and "disabled" in button.group(0), state
        assert 'class="control-reason"' in rendered[state], state
    assert "disabled" not in re.search(r'<button[^>]*id="expand-prompt"[^>]*>', rendered["open"]).group(0)
    # The creative intent is never labelled as the H3 prompt, and vice versa: this panel's whole
    # difficulty is that the two are different things.
    assert "Creative intent" in rendered["expanded"]
    assert "H3 structured prompt" in rendered["expanded"]


def test_pressing_expand_prompt_sends_the_purpose_built_route_and_writes_no_shot_list():
    """Its own route and no body. The shots write is the generic whole-list one, so a request
    meaning "expand this one" through it would reassert every prompt, window and lock this client
    happens to be holding -- which is how a stale client silently reverts the plan."""
    applied = {
        "project": {
            "id": "p1", "jobs": [], "song": None, "assets": [], "messages": [],
            "shots": [json.loads(expansion_shot(h3_prompt=H3_EXPANSION))],
        },
        "applied": True, "problems": [], "prompt": H3_EXPANSION, "note": "",
    }
    result = run_workspace(f"""
      state.health = {{ llm: {{ configured: true }} }};
      state.project = {{ id: 'p1', jobs: [], song: null, assets: [], messages: [],
                        shots: [{expansion_shot()}] }};
      state.selectedShotId = 'shot_a';
      app.renderShotInspector();
      requests.length = 0;
      await fire('#expand-prompt:click', {{}});
      await flush();
      console.log(JSON.stringify({{
        requests: requests.map((item) => ({{ path: item.path, method: item.method, body: item.body }})),
        html: at('#shot-inspector').innerHTML,
      }}));
    """, {"/api/projects/p1/shots/shot_a/expand-prompt": {"body": applied}})

    assert [item["path"] for item in result["requests"]] == [
        "/api/projects/p1/shots/shot_a/expand-prompt"
    ]
    assert result["requests"][0]["method"] == "POST"
    assert result["requests"][0]["body"] is None
    # The reply is adopted, so the panel now shows the expansion and offers to replace it.
    assert 'id="shot-h3-prompt"' in result["html"]
    assert "Expand prompt again" in result["html"]


def test_a_refused_expansion_is_reported_in_the_panel_and_the_intent_is_untouched():
    """The whole reason a refused answer is returned rather than dropped.

    A toast carrying five checker sentences and a thousand characters of returned prompt is a toast
    nobody reads, so the report is drawn under the intent, where the Director can act on it.
    """
    refused = {
        "project": {
            "id": "p1", "jobs": [], "song": None, "assets": [], "messages": [],
            "shots": [json.loads(expansion_shot())],
        },
        "applied": False,
        "problems": ["integrated_multimodal_description is missing.", "No [Shot 1] opening."],
        "prompt": "A grey wolf pacing through trees. 35mm, grainy.",
        "note": "The model's answer is not a well-formed H3 prompt, so it was not saved.",
    }
    result = run_workspace(f"""
      state.health = {{ llm: {{ configured: true }} }};
      state.project = {{ id: 'p1', jobs: [], song: null, assets: [], messages: [],
                        shots: [{expansion_shot()}, {expansion_shot(id="shot_b", start=5)}] }};
      state.selectedShotId = 'shot_a';
      app.renderShotInspector();
      await fire('#expand-prompt:click', {{}});
      await flush();
      const refusedHtml = at('#shot-inspector').innerHTML;
      // ...and the report does not follow the Director to the next shot.
      state.selectedShotId = 'shot_b';
      app.renderShotInspector();
      console.log(JSON.stringify({{ refusedHtml, otherHtml: at('#shot-inspector').innerHTML }}));
    """, {"/api/projects/p1/shots/shot_a/expand-prompt": {"body": refused}})

    assert 'id="expansion-report"' in result["refusedHtml"]
    for problem in refused["problems"]:
        assert problem in result["refusedHtml"], problem
    # The refused text itself, so it can be read and judged.
    assert "A grey wolf pacing through trees. 35mm, grainy." in result["refusedHtml"]
    # Nothing was stored, so no expansion box is drawn and the intent is still the intent.
    assert 'id="shot-h3-prompt"' not in result["refusedHtml"]
    assert "A wolf crosses the clearing" in result["refusedHtml"]
    # Keyed to its shot: a failure drawn under a different shot's intent would be a false claim
    # about the panel it sits in.
    assert 'id="expansion-report"' not in result["otherHtml"]


def test_editing_the_expansion_writes_h3_prompt_and_leaves_the_intent_alone():
    """The box saves through the ordinary shots write, so this follows the edit onto the wire.

    `h3_prompt` is the one field this client otherwise carries round-trip without ever touching, so
    a bug in the read-back is invisible until a render submits the wrong document.
    """
    written = run_workspace(f"""
      state.project = {{ id: 'p1', jobs: [], song: null, assets: [],
                        shots: [{expansion_shot(h3_prompt=H3_EXPANSION)}] }};
      state.selectedShotId = 'shot_a';
      app.renderShotInspector();
      // The stub renders markup rather than parsing it, so the boxes the handler reads back have
      // to be seeded the way the browser would have. The intent is seeded to what it already says,
      // which is the point: an edit to the expansion must leave it exactly there.
      at('#shot-prompt').value = 'A wolf crosses the clearing';
      at('#shot-h3-prompt').value = 'integrated_multimodal_description: [Shot 1] Edited by hand.';
      requests.length = 0;
      fire('#shot-h3-prompt:change', {{}});
      await flush();
      const edited = JSON.parse(requests[requests.length - 1].body).shots[0];
      console.log(JSON.stringify({{ h3: edited.h3_prompt, prompt: edited.prompt }}));
    """)

    assert written["h3"] == "integrated_multimodal_description: [Shot 1] Edited by hand."
    assert written["prompt"] == "A wolf crosses the clearing"


def test_an_ordinary_edit_never_copies_one_shots_expansion_onto_another():
    """The mutation this guards: reading `#shot-h3-prompt` back unconditionally.

    The box is drawn only for a shot that has an expansion, so the read-back has to be conditional
    -- and the condition has to be the *shot's own field*, not whether the element is reachable. An
    unguarded read takes whatever that box last held, which after looking at an expanded shot is
    that shot's expansion. An unrelated edit on the next shot -- a seed, a checkbox -- would then
    write it onto that one through the whole-list save, and nothing on screen would say so until a
    render submitted the wrong document.

    Driven exactly that way: look at the expanded shot, then at the plain one, then nudge its seed.
    """
    written = run_workspace(f"""
      state.project = {{ id: 'p1', jobs: [], song: null, assets: [],
                        shots: [{expansion_shot(h3_prompt=H3_EXPANSION)},
                                {expansion_shot(id="shot_b", start=5, prompt="Lucy turns")}] }};
      state.selectedShotId = 'shot_a';
      app.renderShotInspector();
      // What the browser would have put in the box for the expanded shot.
      at('#shot-h3-prompt').value = {json.dumps(H3_EXPANSION)};
      state.selectedShotId = 'shot_b';
      app.renderShotInspector();
      at('#shot-prompt').value = 'Lucy turns';
      at('#shot-seed').value = '7';
      requests.length = 0;
      fire('#shot-seed:change', {{}});
      await flush();
      const shots = JSON.parse(requests[requests.length - 1].body).shots;
      console.log(JSON.stringify({{
        plain: {{ h3: shots[1].h3_prompt, seed: shots[1].seed, prompt: shots[1].prompt }},
        expanded: shots[0].h3_prompt,
      }}));
    """)

    assert written["plain"]["h3"] == ""
    assert written["plain"]["seed"] == 7
    assert written["plain"]["prompt"] == "Lucy turns"
    # ...and the shot that really does have one still has it: the whole-list save carries it
    # round-trip untouched, which is the other half of the same guarantee.
    assert written["expanded"] == H3_EXPANSION
    # And what the server would make of that body is a valid Shot rather than a validation error.
    assert Shot.model_validate(
        {"start": 0, "duration": 5, "h3_prompt": written["plain"]["h3"]}
    ).h3_prompt == ""


def test_the_sweep_control_is_wired_to_its_own_route_and_says_what_it_costs():
    source = APP_JS.read_text(encoding="utf-8")
    markup = INDEX_HTML.read_text(encoding="utf-8")
    handler = app_js_block("async function expandPlanPrompts")

    button = re.search(r'<button[^>]*id="expand-h3-prompts"[^>]*>([^<]*)</button>', markup)
    assert button, "the plan-wide H3 sweep has no control in the markup"
    # It ships disabled: only `syncExpansionControls` enables it, off the pure control function, so
    # a plan with no shots never offers a live button.
    assert "disabled" in button.group(0), button.group(0)
    assert '$(EXPAND_ALL_PROMPTS_CONTROL).addEventListener("click", expandPlanPrompts);' in source
    # One route, and only that one. A handler that also called the shots write would reassert the
    # whole plan under a request meaning "expand it".
    assert re.findall(r"api\.(\w+)\(", handler) == ["expandPlanPrompts"], handler
    # The same two protections the pass-one expansion has, and in the same order: this call is N
    # model calls rather than one, so every window it leaves open is open N times as long.
    assert handler.index('shotWriteInFlight = "expansion";') < handler.index("await shotSaveChain")
    assert handler.index("await shotSaveChain") < handler.index("api.expandPlanPrompts(")
    assert RELEASED_IN_FINALLY.search(handler), handler
    # The reply is adopted only if the Director is still looking at the project it answers.
    assert handler.index("api.expandPlanPrompts(") < handler.index("state.project?.id !== projectId")
    assert handler.index("state.project?.id !== projectId") < handler.index("state.project = expanded")


def test_the_sweep_control_carries_the_api_js_label_and_help_and_the_servers_refusal():
    """The markup cannot import them, so the two halves are asserted equal here -- and the empty
    plan refusal is the server's own sentence rather than a second wording of one rule."""
    from music_video_producer.app import EXPAND_PROMPTS_WITHOUT_SHOTS

    strings = run_module("""
      import { EXPAND_ALL_PROMPTS_LABEL, EXPAND_ALL_PROMPTS_HELP, EXPAND_ALL_PROMPTS_WITHOUT_SHOTS,
               SHOT_EXPANSION_NO_RENDER, expandAllPromptsControl }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        label: EXPAND_ALL_PROMPTS_LABEL,
        help: EXPAND_ALL_PROMPTS_HELP,
        refusal: EXPAND_ALL_PROMPTS_WITHOUT_SHOTS,
        noRender: SHOT_EXPANSION_NO_RENDER,
        empty: expandAllPromptsControl({ shots: [] }),
        missing: expandAllPromptsControl(null),
        planned: expandAllPromptsControl({ shots: [{ id: 's', locked: true }] }),
      }));
    """)
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert strings["refusal"] == EXPAND_PROMPTS_WITHOUT_SHOTS
    button = re.search(r'<button[^>]*id="expand-h3-prompts"[^>]*>([^<]*)</button>', markup)
    assert button.group(1) == strings["label"]
    # What pressing it costs is stated before the click, in the one spelling every other control
    # here uses for it.
    assert strings["noRender"] in strings["help"]
    assert "one call per shot" in strings["help"]
    # Off only when there is nothing to sweep. A plan whose shots are merely locked is *not*
    # filtered here: this route sends no selection, so filtering would be the client deciding what
    # the report says about shots it never mentioned.
    assert strings["empty"] == {"disabled": True, "title": strings["refusal"]}
    assert strings["missing"]["disabled"] is True
    assert strings["planned"] == {"disabled": False, "title": strings["help"]}


def test_the_sweep_toast_reads_its_count_out_of_the_servers_own_notice():
    """Read from the reply, never diffed off the shots: `h3_prompt` is not drawn on the timeline at
    all, so a diff would have nothing on screen to be checked against."""
    from music_video_producer.app import EXPAND_PROMPTS_WRITTEN_NOTICE

    notice = EXPAND_PROMPTS_WRITTEN_NOTICE.format(count=3, shots="SHOT 01 (a), SHOT 02 (b), SHOT 03 (c)")
    project = Project(name="Swept")
    project.messages = [
        MessageNotice and TreatmentMessage(role="assistant", content=f"Ran the specialist.\n\n{notice}")
    ]
    unchanged = Project(name="Nothing")
    unchanged.messages = [TreatmentMessage(role="assistant", content="Ran the specialist.")]

    toasts = run_module(f"""
      import {{ expandAllPromptsToast, expandAllPromptsWritten }}
        from './src/music_video_producer/web/assets/api.js';
      const swept = {json.dumps(json.loads(project.model_dump_json()))};
      const untouched = {json.dumps(json.loads(unchanged.model_dump_json()))};
      console.log(JSON.stringify({{
        count: expandAllPromptsWritten(swept),
        toast: expandAllPromptsToast(swept),
        unchanged: expandAllPromptsToast(untouched),
      }}));
    """)

    assert toasts["count"] == 3
    assert toasts["toast"].startswith("3 H3 prompts written")
    assert "reply says per shot" in toasts["toast"]
    assert "No H3 prompt was written" in toasts["unchanged"]


def test_the_two_expansion_toasts_do_not_read_each_others_markers():
    """Pass one writes "Prompts written for N shot(s):" and pass two "H3 prompts written for N".

    One is a substring of the other in every sense that matters to a reader, so this asserts each
    extractor ignores the other's notice -- otherwise a sweep would toast a pass-one count, or a
    pass-one expansion would claim H3 prompts nobody wrote.
    """
    from music_video_producer.app import EXPAND_PROMPTS_WRITTEN_NOTICE, EXPANSION_WRITTEN_NOTICE

    def project_with(notice: str) -> dict:
        held = Project(name="Marker")
        held.messages = [TreatmentMessage(role="assistant", content=notice)]
        return json.loads(held.model_dump_json())

    counts = run_module(f"""
      import {{ expandAllPromptsWritten, shotExpansionWritten }}
        from './src/music_video_producer/web/assets/api.js';
      const passOne = {json.dumps(project_with(EXPANSION_WRITTEN_NOTICE.format(count=4, shots="a")))};
      const passTwo = {json.dumps(project_with(EXPAND_PROMPTS_WRITTEN_NOTICE.format(count=2, shots="a")))};
      console.log(JSON.stringify({{
        oneReadsOne: shotExpansionWritten(passOne),
        oneReadsTwo: shotExpansionWritten(passTwo),
        twoReadsTwo: expandAllPromptsWritten(passTwo),
        twoReadsOne: expandAllPromptsWritten(passOne),
      }}));
    """)

    assert counts == {"oneReadsOne": 4, "oneReadsTwo": 0, "twoReadsTwo": 2, "twoReadsOne": 0}


# --------------------------------------------------------------------------------------------
# Render polling -- the client half of AD-1, executed rather than read.
# --------------------------------------------------------------------------------------------


def poll_project(**overrides) -> str:
    """One project mid-Flux-render, as `state.project` holds it, for the workspace tests."""
    project = {
        "id": "p1", "name": "Poll", "song": None, "shots": [], "messages": [],
        "assets": [{
            "id": "a1", "name": "Lead singer", "kind": "character", "path": "",
            "source": "flux", "prompt_id": "pr1", "created_at": "2026-08-18T00:00:00Z",
        }],
        "jobs": [{
            "id": "j1", "kind": "flux", "status": "queued", "prompt_id": "pr1",
            "target_id": "a1", "seed": 7, "output_files": [], "error": "",
        }],
    }
    project.update(overrides)
    return json.dumps(project)


#: The poll answer that settles that render, in the route's fixed shape.
POLL_COMPLETION = {
    "active": False,
    "comfy_online": True,
    "jobs": [{
        "id": "j1", "kind": "flux", "status": "complete", "prompt_id": "pr1",
        "target_id": "a1", "seed": 7,
        "output_files": ["music-video-producer/p1/assets/singer_00001_.png"], "error": "",
    }],
    "shots": [],
    "assets": [{"asset_id": "a1", "path": "music-video-producer/p1/assets/singer_00001_.png"}],
    "song": None,
}


def test_the_polling_constants_mirror_the_server_and_the_ad():
    """AD-1 in numbers: a 2 s interval, and the same definition of "settled" on both sides."""
    offered = run_module("""
      import { RENDER_POLL_INTERVAL_MS, TERMINAL_JOB_STATUSES }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({ RENDER_POLL_INTERVAL_MS, TERMINAL_JOB_STATUSES }));
    """)

    assert offered["RENDER_POLL_INTERVAL_MS"] == 2000
    assert set(offered["TERMINAL_JOB_STATUSES"]) == set(TERMINAL_JOB_STATUSES)


def test_both_languages_agree_on_which_projects_have_renders_in_flight():
    """`hasActiveRenderJobs` against `batch.reconcilable_jobs`, over every status × prompt-id.

    This predicate is the whole polling contract -- the browser polls exactly while it is true
    -- so the two sides disagreeing is a client that polls an idle project forever, or one that
    never sees a live job finish."""
    cases = []
    for status in ("queued", "running", "complete", "error", "cancelled"):
        for prompt_id in ("pr1", ""):
            cases.append({
                "id": f"j_{status}_{'with' if prompt_id else 'without'}",
                "kind": "flux", "status": status, "prompt_id": prompt_id, "target_id": "a",
            })
    projects = [{"jobs": [case]} for case in cases] + [{"jobs": []}, {"jobs": cases}]

    answers = run_module(f"""
      import {{ hasActiveRenderJobs }} from './src/music_video_producer/web/assets/api.js';
      const projects = {json.dumps(projects)};
      console.log(JSON.stringify(projects.map(hasActiveRenderJobs)));
    """)

    for answer, held in zip(answers, projects, strict=True):
        project = Project(name="Parity")
        project.jobs = [RenderJob.model_validate(job) for job in held["jobs"]]
        assert answer == bool(reconcilable_jobs(project)), held
    # The matrix produces both answers, or this pins nothing.
    assert True in answers and False in answers


def test_apply_render_status_patches_render_facts_and_only_render_facts():
    """The 2 s patch, executed over the hazards it exists to avoid.

    A poll answer can be a request older than a click the Director just made, and the whole-list
    shots save re-asserts every local field -- so what matters as much as the completion landing
    is everything the patch refuses to touch: a settled job never regresses, a draft/ready shot
    is never moved, a landed asset is never un-landed, another Song's audio is never adopted,
    and a job the report predates is kept so polling does not stop watching it."""
    verdict = run_module(f"""
      import {{ applyRenderStatus }} from './src/music_video_producer/web/assets/api.js';
      const report = {json.dumps(POLL_COMPLETION)};
      const completion = {{
        project: {{
          id: 'p1',
          assets: [{{ id: 'a1', name: 'Lead singer', path: '', prompt_id: 'pr1' }}],
          shots: [], song: null,
          jobs: [{{ id: 'j1', kind: 'flux', status: 'queued', prompt_id: 'pr1', target_id: 'a1',
                   output_files: [], error: '' }}],
        }},
      }};
      const completionChanges = applyRenderStatus(completion.project, report);

      const guarded = {{
        project: {{
          id: 'p1',
          assets: [{{ id: 'a1', name: 'Lead singer', path: 'already/landed_00001_.png' }}],
          shots: [
            {{ id: 's_ready', status: 'ready', prompt: 'typed', latest_output: '', latest_review: null }},
            {{ id: 's_running', status: 'running', latest_output: 'old/take_00001.mp4',
               latest_review: {{ summary: 'previous take' }} }},
          ],
          song: {{ title: 'Mine', path: 'songs/mine.flac', prompt_id: 'song-mine' }},
          jobs: [
            {{ id: 'j_done', kind: 'h3', status: 'complete', prompt_id: 'pr-done', target_id: 's_ready',
               output_files: ['kept.mp4'], error: '' }},
            {{ id: 'j_new', kind: 'h3', status: 'queued', prompt_id: 'pr-new', target_id: 's_x',
               output_files: [], error: '' }},
          ],
        }},
      }};
      const staleReport = {{
        active: false, comfy_online: true,
        jobs: [
          // A stale snapshot claiming the settled job is still running: must not regress it.
          {{ id: 'j_done', kind: 'h3', status: 'running', prompt_id: 'pr-done', target_id: 's_ready',
             output_files: [], error: '' }},
          // j_new is absent: the report predates its submission and must not delete it.
          // And a job this client has never seen is adopted, not dropped.
          {{ id: 'j_other', kind: 'h3', status: 'running', prompt_id: 'pr-other', target_id: 's_running',
             output_files: [], error: '' }},
        ],
        shots: [
          // A stale 'draft' over the Director's fresh 'ready': refused.
          {{ shot_id: 's_ready', status: 'draft', latest_output: '' }},
          // A real completion of an in-flight shot: applied, and the old review displaced.
          {{ shot_id: 's_running', status: 'complete', latest_output: 'new/take_00002.mp4' }},
        ],
        assets: [
          // An empty path over a landed file is what a stale snapshot says about an upload
          // it predates: refused.
          {{ asset_id: 'a1', path: '' }},
        ],
        song: {{ path: 'songs/other_00001_.flac', prompt_id: 'song-other' }},
      }};
      const guardedChanges = applyRenderStatus(guarded.project, staleReport);

      console.log(JSON.stringify({{
        completionChanges,
        completedAsset: completion.project.assets[0].path,
        completedJob: completion.project.jobs[0].status,
        guardedChanges,
        keptJob: guarded.project.jobs.find((job) => job.id === 'j_done').status,
        keptNewJob: Boolean(guarded.project.jobs.find((job) => job.id === 'j_new')),
        adoptedJob: Boolean(guarded.project.jobs.find((job) => job.id === 'j_other')),
        readyShot: guarded.project.shots[0].status,
        landedShot: {{
          status: guarded.project.shots[1].status,
          output: guarded.project.shots[1].latest_output,
          review: guarded.project.shots[1].latest_review,
        }},
        keptAsset: guarded.project.assets[0].path,
        keptSong: guarded.project.song.path,
      }}));
    """)

    assert verdict["completionChanges"]["jobs"] is True
    assert verdict["completionChanges"]["assets"] is True
    assert [job["id"] for job in verdict["completionChanges"]["settled"]] == ["j1"]
    assert verdict["completedAsset"] == "music-video-producer/p1/assets/singer_00001_.png"
    assert verdict["completedJob"] == "complete"

    assert verdict["keptJob"] == "complete"
    assert verdict["keptNewJob"] is True
    assert verdict["adoptedJob"] is True
    assert verdict["readyShot"] == "ready"
    assert verdict["landedShot"] == {
        "status": "complete", "output": "new/take_00002.mp4", "review": None,
    }
    assert verdict["keptAsset"] == "already/landed_00001_.png"
    assert verdict["keptSong"] == "songs/mine.flac"
    assert verdict["guardedChanges"]["settled"] == []


def test_the_poll_carries_the_measurement_onto_the_job_it_just_settled():
    """The Took column, blank at the only moment anybody is looking at it.

    `RenderStatusReport.jobs` is a list of whole `RenderJob`s, so the measurement is on the wire on
    the very tick a render settles -- and the patch copied `status`, `output_files` and `error`
    and nothing else. Every render the Director actually watches finish therefore drew `—`, under
    a tooltip saying no timing was ever taken for it, about a job measured 200 ms earlier. Nothing
    repaired it: the poll stands itself down when the last job settles and never calls
    `loadProject`, so the blank stood until the next project switch.

    Executed rather than asserted about the field list, because the defect *was* a field list: a
    test that reads which names appear in the merge is a test that would have been written from
    the same three names.
    """
    measured = json.loads(RenderJob(
        id="j1", kind="h3", prompt_id="pr1", target_id="s1", status="complete",
        output_files=["take_00002.mp4"], render_seconds=378.0, render_seconds_source="comfy",
        render_frames=141,
    ).model_dump_json())
    verdict = run_workspace(f"""
      const held = {{
        id: 'p1', shots: [], assets: [], song: null,
        jobs: [{{ id: 'j1', kind: 'h3', status: 'running', prompt_id: 'pr1', target_id: 's1',
                 output_files: [], error: '', render_seconds: 0, render_seconds_source: '',
                 render_frames: 0 }}],
      }};
      const report = {{ active: false, comfy_online: true, shots: [], assets: [], song: null,
                       jobs: [{json.dumps(measured)}] }};
      const moved = contract.applyRenderStatus(held, report);
      const settled = held.jobs[0];
      // The second tick learns nothing and must say so, or the panel repaints every two seconds.
      const again = contract.applyRenderStatus(held, report);

      // Ticks on which *only* the measurement moved, one field at a time. The "did this answer
      // teach us anything" test has to cover every field the merge writes, or a field can move on
      // the wire and never reach the screen -- which is the defect above, one level up. One field
      // at a time because three at once is satisfied by any one of the three comparisons.
      const openJob = (over) => ({{ id: 'j2', kind: 'h3', status: 'running', prompt_id: 'pr2',
        target_id: 's2', output_files: [], error: '', render_seconds: 92.5,
        render_seconds_source: 'comfy', render_frames: 141, ...over }});
      const answered = {{ active: true, comfy_online: true, shots: [], assets: [], song: null,
        jobs: [openJob({{}})] }};
      const late = {{}};
      for (const [field, stale] of [['render_seconds', 0], ['render_seconds_source', ''],
                                    ['render_frames', 0]]) {{
        const open = {{ id: 'p1', shots: [], assets: [], song: null,
                       jobs: [openJob({{ [field]: stale }})] }};
        const changes = contract.applyRenderStatus(open, answered);
        late[field] = {{ moved: changes.jobs, settled: changes.settled.length,
                        held: open.jobs[0][field] }};
      }}

      console.log(JSON.stringify({{
        moved: moved.jobs,
        again: again.jobs,
        settled,
        cell: app.renderTimingCell(settled),
        sentence: app.renderTimingSummary(settled),
        late,
      }}));
    """)

    assert verdict["moved"] is True
    assert verdict["again"] is False
    assert verdict["late"] == {
        # Each field alone: the tick moved, and the held job carries the report's value after it.
        # Still `running`, so nothing announces a finished render off a measurement alone.
        "render_seconds": {"moved": True, "settled": 0, "held": 92.5},
        "render_seconds_source": {"moved": True, "settled": 0, "held": "comfy"},
        "render_frames": {"moved": True, "settled": 0, "held": 141},
    }
    assert verdict["settled"]["render_seconds"] == 378.0
    assert verdict["settled"]["render_seconds_source"] == "comfy"
    assert verdict["settled"]["render_frames"] == 141
    # And the two surfaces the Director reads, drawn off the patched job rather than off a reload.
    assert verdict["cell"] == "6m18s · 141f"
    assert verdict["sentence"] == render_timing_summary(
        RenderJob.model_validate(measured)
    ) == "rendered in 6m18s, 141 frames"


def test_settled_toasts_name_the_target_and_never_dress_an_error_as_good_news():
    sentences = run_module("""
      import { renderSettledToast } from './src/music_video_producer/web/assets/api.js';
      const project = {
        assets: [{ id: 'a1', name: 'Lead singer' }],
        shots: [{ id: 's1' }],
        song: { title: 'Night Signal' },
      };
      console.log(JSON.stringify({
        flux: renderSettledToast(project, { kind: 'flux', status: 'complete', target_id: 'a1' }),
        h3: renderSettledToast(project, { kind: 'h3', status: 'complete', target_id: 's1' }),
        music: renderSettledToast(project, { kind: 'music', status: 'complete', target_id: 'song' }),
        failed: renderSettledToast(project, { kind: 'flux', status: 'error', target_id: 'a1',
                                              error: 'KSampler: out of memory' }),
      }));
    """)

    assert sentences["flux"] == "Render complete: Lead singer is ready"
    assert sentences["h3"] == "Render complete: SHOT 01 (s1) is ready"
    assert sentences["music"] == "Render complete: Night Signal is ready"
    assert sentences["failed"].startswith("Render failed for Lead singer")
    assert "KSampler: out of memory" in sentences["failed"]
    assert "complete" not in sentences["failed"]


def test_the_poll_timer_exists_exactly_while_the_project_has_open_jobs():
    """The interval is scheduled at AD-1's 2 s when a job is open, never doubled, stood down
    when the last job settles, and never scheduled for an idle or absent project."""
    timers = run_workspace(f"""
      const scheduled = [];
      let cleared = 0;
      let nextHandle = 0;
      globalThis.setInterval = (fn, ms) => {{ scheduled.push(ms); nextHandle += 1; return nextHandle; }};
      globalThis.clearInterval = () => {{ cleared += 1; }};

      state.project = null;
      app.syncRenderPolling();
      const withoutProject = {{ scheduled: [...scheduled], cleared }};

      state.project = {json.dumps(json.loads(poll_project()))};
      app.syncRenderPolling();
      const started = {{ scheduled: [...scheduled], cleared }};
      app.syncRenderPolling();
      const steady = {{ scheduled: [...scheduled], cleared }};

      state.project.jobs[0].status = 'complete';
      app.syncRenderPolling();
      const stopped = {{ scheduled: [...scheduled], cleared }};

      // A job that is open but has no prompt id has nothing to poll for.
      state.project.jobs = [{{ id: 'j2', kind: 'post', status: 'queued', prompt_id: '', target_id: 'x' }}];
      app.syncRenderPolling();
      const unpollable = {{ scheduled: [...scheduled], cleared }};

      console.log(JSON.stringify({{ withoutProject, started, steady, stopped, unpollable }}));
    """)

    assert timers["withoutProject"] == {"scheduled": [], "cleared": 0}
    assert timers["started"] == {"scheduled": [2000], "cleared": 0}
    assert timers["steady"] == {"scheduled": [2000], "cleared": 0}
    assert timers["stopped"] == {"scheduled": [2000], "cleared": 1}
    assert timers["unpollable"] == {"scheduled": [2000], "cleared": 1}


def test_a_poll_tick_lands_a_completion_on_every_surface_without_a_click():
    """The live defect, executed: the tick alone moves the asset card off RENDERING, marks the
    job row complete, and says out loud that the render finished."""
    landed = run_workspace("""
      const toasts = [];
      at('#toast-region').append = (item) => toasts.push(item.textContent);
      state.project = __PROJECT__;
      await flush();
      requests.length = 0;

      await app.pollRenderStatus();
      await flush();

      console.log(JSON.stringify({
        polled: requests.map((entry) => ({ path: entry.path, method: entry.method })),
        assetPath: state.project.assets[0].path,
        grid: at('#asset-grid').innerHTML,
        jobs: at('#job-list').innerHTML,
        toasts,
      }));
    """.replace("__PROJECT__", poll_project()),
        responses={"/api/projects/p1/render-status": {"body": POLL_COMPLETION}},
    )

    assert landed["polled"] == [{"path": "/api/projects/p1/render-status", "method": "GET"}]
    assert landed["assetPath"] == "music-video-producer/p1/assets/singer_00001_.png"
    # The card now carries the image where RENDERING stood, drawn by the tick and nothing else.
    assert "RENDERING" not in landed["grid"]
    assert "singer_00001_.png" in landed["grid"]
    assert '<img src=' in landed["grid"]
    assert 'complete' in landed["jobs"]
    assert landed["toasts"] == ["Render complete: Lead singer is ready"]


def test_an_idle_project_and_a_guarded_write_each_produce_zero_poll_requests():
    """The tick's three refusals, driven: no open jobs means no request at all, and a shot write
    in flight (an expansion holding its read-to-save window open) skips the tick rather than
    interleaving with it -- the docs/LLM-DIRECTOR.md guard, honoured by the poll."""
    quiet = run_workspace("""
      state.project = __PROJECT__;
      state.project.jobs[0].status = 'complete';
      await flush();
      requests.length = 0;
      await app.pollRenderStatus();
      const idle = requests.length;

      // Re-open the job, then start an expansion whose call never returns: the write flag is up.
      state.project.jobs[0].status = 'queued';
      state.health = { llm: { configured: true } };
      state.project.shots = [{ id: 's1', start: 0, duration: 5, prompt: 'A singer turns',
        mode: null, citations: [], asset_ids: [], singing: 'unknown', seed: 0, status: 'draft',
        prompt_id: '', latest_output: '', approved_output: '', locked: false, h3_prompt: '' }];
      state.selectedShotId = 's1';
      app.renderShotInspector();
      const settle = globalThis.fetch;
      globalThis.fetch = (path, options = {}) => {
        if (path.endsWith('/expand-prompt')) return new Promise(() => {});
        return settle(path, options);
      };
      requests.length = 0;
      fire('#expand-prompt:click');
      await app.pollRenderStatus();
      const guarded = requests.filter((entry) => entry.path.includes('render-status')).length;

      console.log(JSON.stringify({ idle, guarded }));
    """.replace("__PROJECT__", poll_project()),
        responses={"/api/projects/p1/render-status": {"body": POLL_COMPLETION}},
    )

    assert quiet == {"idle": 0, "guarded": 0}


def test_the_manual_refresh_reconciles_once_instead_of_fanning_out_per_job():
    """The queue panel's Refresh, rewired: one render-status call and one project reload, and
    never the per-job GET fan-out AD-1 calls out (forty jobs were forty queue reads)."""
    refreshed = run_workspace("""
      const scheduled = [];
      globalThis.setInterval = (fn, ms) => { scheduled.push(ms); return scheduled.length; };
      state.project = __PROJECT__;
      state.project.jobs = Array.from({ length: 5 }, (_, index) => ({
        id: 'j' + index, kind: 'flux', status: 'queued', prompt_id: 'pr' + index,
        target_id: 'a1', seed: 0, output_files: [], error: '',
      }));
      await flush();
      requests.length = 0;
      scheduled.length = 0;
      await fire('#refresh-jobs:click', {});
      await flush();
      console.log(JSON.stringify({ paths: requests.map((entry) => entry.path), scheduled }));
    """.replace("__PROJECT__", poll_project()),
        responses={
            "/api/projects/p1/render-status": {"body": POLL_COMPLETION},
            "/api/projects/p1": {"body": json.loads(poll_project())},
        },
    )

    paths = refreshed["paths"]
    assert paths.count("/api/projects/p1/render-status") == 1
    assert "/api/projects/p1" in paths
    assert not any("/jobs/" in path for path in paths), paths
    # The reload repainted the queue panel, and the panel's render is what schedules the poll:
    # a project arriving with an open job starts the 2 s loop without any handler knowing it.
    assert refreshed["scheduled"] == [2000]


def test_generation_submits_are_shut_while_their_own_request_is_in_flight():
    """The double-render's other half: the Flux form holds a fixed seed, so the live submit
    button during the silent seconds was an invitation to queue the identical image twice. The
    button is executed through both arms -- held shut while the request hangs, restored with its
    own label when the request settles."""
    driven = run_workspace("""
      const NAMES = ['name', 'kind', 'prompt', 'aspect', 'steps', 'guidance', 'seed'];
      globalThis.FormData = class {
        constructor(form) {
          this.pairs = NAMES.map((name) => [name, form.elements[name].value]);
        }
        [Symbol.iterator]() { return this.pairs[Symbol.iterator](); }
      };
      const form = at('#flux-form');
      form.elements.name.value = 'Lead singer';
      form.elements.kind.value = 'character';
      form.elements.prompt.value = 'portrait';
      form.elements.aspect.value = '1024x1024';
      form.elements.steps.value = '4';
      form.elements.guidance.value = '4';
      form.elements.seed.value = '7';
      state.project = __PROJECT__;
      await flush();

      const button = at('#flux-submit');
      button.textContent = 'Generate with Flux';

      // The failing arm restores the control: a refused submission must not leave it dead.
      requests.length = 0;
      await fire('#flux-form:submit', { preventDefault() {}, currentTarget: form });
      await flush();
      const restored = { disabled: button.disabled, label: button.textContent, sent: requests.length };

      // The hanging arm holds it shut -- and a second submission fired anyway (nothing obliges
      // every event path to be a browser honouring the disabled attribute) is refused without
      // reaching the network.
      const settle = globalThis.fetch;
      globalThis.fetch = (path, options = {}) => {
        requests.push({ path, method: options.method || 'GET' });
        if (path.endsWith('/generate/flux')) return new Promise(() => {});
        return Promise.reject(new Error('no'));
      };
      requests.length = 0;
      fire('#flux-form:submit', { preventDefault() {}, currentTarget: form });
      const inFlight = { disabled: button.disabled, label: button.textContent, sent: requests.length };
      fire('#flux-form:submit', { preventDefault() {}, currentTarget: form });
      const refused = { disabled: button.disabled, label: button.textContent, sent: requests.length };

      const musicHandlerBound = Boolean(listeners.get('#music-form:submit'));
      console.log(JSON.stringify({ restored, inFlight, refused, musicHandlerBound }));
    """.replace("__PROJECT__", poll_project()))

    assert driven["restored"] == {"disabled": False, "label": "Generate with Flux", "sent": 1}
    assert driven["inFlight"] == {"disabled": True, "label": "Queuing…", "sent": 1}
    # The second click while in flight queued nothing: the identical-seed double render is shut.
    assert driven["refused"] == {"disabled": True, "label": "Queuing…", "sent": 1}
    assert driven["musicHandlerBound"] is True

    # The music form's submit carries the same protection, asserted in its source because its
    # driven test (test_the_song_form_sends_the_headroom_it_shows...) already executes the
    # handler end to end and would fail on an unrestored button.
    handler = without_comments(app_js_block('musicForm.addEventListener("submit"', "\n  });"))
    assert '$("#music-submit")' in handler
    assert "button.disabled = true" in handler
    assert "finally { button.disabled = false; button.textContent = label; }" in handler


def test_section_snapping_is_executed_and_the_track_is_interactive():
    """The Director's section boxes: `snapSeconds` pulls an edge to the nearest shot
    boundary within tolerance and otherwise leaves it free; `shotBoundaries` collects
    every edge a box may land on. The wiring assertions pin the interactive track —
    pills bound like clips, dblclick-create on empty space, the inspector branch — so
    the boxes cannot silently regress to the read-only pills they replaced."""
    states = run_module("""
      import { shotBoundaries, snapSeconds } from './src/music_video_producer/web/assets/api.js';
      const project = { song: { duration: 60 }, shots: [
        { start: 0, duration: 6 }, { start: 6, duration: 6 }, { start: 12, duration: 5.5 },
      ]};
      const edges = shotBoundaries(project);
      console.log(JSON.stringify({
        edges,
        near: snapSeconds(5.8, edges, 0.4),
        exact: snapSeconds(12, edges, 0.4),
        far: snapSeconds(9.1, edges, 0.4),
        nothing: snapSeconds(3, [], 0.4),
        ties: snapSeconds(6.1, edges, 0.4),
      }));
    """)
    assert states["edges"] == [0, 6, 12, 17.5, 60]
    assert states["near"] == 6
    assert states["exact"] == 12
    assert states["far"] == 9.1  # nothing within tolerance: free
    assert states["nothing"] == 3
    assert states["ties"] == 6

    workspace = APP_JS.read_text(encoding="utf-8")
    assert '$$("#section-track .section-pill").forEach(bindSection);' in workspace
    assert "function bindSection(pill)" in workspace
    assert "snapSeconds(" in workspace
    assert 'addEventListener("dblclick"' in workspace
    assert "state.selectedSectionId" in workspace
    # The inspector owns the shared prompt when a section is selected.
    assert 'id="section-prompt"' in workspace
    assert 'id="section-delete"' in workspace
    # Selecting a shot clears the section selection and vice versa — one panel, one owner.
    assert "state.selectedSectionId = null;" in workspace


# ------------------------------------------------------------------------------------------
# Export presets and assembly progress (Phase 4.2). The select must offer exactly what the
# route accepts, and the progress reader must key on the same local-job marker AD-9 uses.
# ------------------------------------------------------------------------------------------


def export_presets() -> dict:
    """api.js's preset table and its default, plus what `api.assemble` puts on the wire."""
    return run_module("""
      const seen = [];
      globalThis.fetch = (path, options = {}) => {
        seen.push({ path, method: options.method, body: options.body });
        return Promise.resolve({
          ok: true, status: 200,
          headers: { get: () => 'application/json' },
          json: async () => ({ preset: 'draft' }),
        });
      };
      const { api, EXPORT_PRESETS, EXPORT_PRESET_DEFAULT }
        = await import('./src/music_video_producer/web/assets/api.js');
      await api.assemble('p1');
      await api.assemble('p1', 'master');
      console.log(JSON.stringify({
        presets: EXPORT_PRESETS,
        fallback: EXPORT_PRESET_DEFAULT,
        requests: seen,
      }));
    """)


def test_the_preset_select_offers_exactly_what_the_route_accepts():
    """A select offering a preset the server refuses is a dead control, and one missing a
    preset the server accepts is a delivery build nobody can reach. Both halves come from
    the same two names, so this holds them together — the client list against the route's
    `Literal`, and the route's `Literal` against `assembly.EXPORT_PRESETS`."""
    from music_video_producer.app import AssemblyRequest
    from music_video_producer.assembly import DEFAULT_EXPORT_PRESET, EXPORT_PRESETS

    accepted = set(get_args(AssemblyRequest.model_fields["preset"].annotation))
    assert accepted == set(EXPORT_PRESETS)

    offered = export_presets()
    assert [preset["value"] for preset in offered["presets"]] == ["draft", "master"]
    assert {preset["value"] for preset in offered["presets"]} == accepted
    assert offered["fallback"] == DEFAULT_EXPORT_PRESET
    assert AssemblyRequest().preset == DEFAULT_EXPORT_PRESET
    # Every option says what it does; a two-word label is not enough to choose between a
    # review build and one that changes the loudness of the delivered file.
    for preset in offered["presets"]:
        assert preset["label"] and len(preset["help"]) > 40


def test_the_assemble_call_always_names_a_preset_and_defaults_to_draft():
    """The client never sends a body-less assemble any more, and the body it does send names
    `draft` unless told otherwise — the same default the route would have applied anyway, so
    the two cannot disagree about what an untouched button produces."""
    requests = export_presets()["requests"]

    assert [request["path"] for request in requests] == [
        "/api/projects/p1/assemble", "/api/projects/p1/assemble"
    ]
    assert [request["method"] for request in requests] == ["POST", "POST"]
    assert json.loads(requests[0]["body"]) == {"preset": "draft"}
    assert json.loads(requests[1]["body"]) == {"preset": "master"}


def test_assembly_progress_reads_the_local_job_and_only_the_local_job():
    """AD-9's marker, read the way `latestAssemblyExport` reads it: kind `post` with an empty
    prompt id. A ComfyUI render — which `hasActiveRenderJobs` does watch — must not be
    mistaken for an export, a settled assembly reports nothing rather than a stale number,
    and a manifest written before the field existed reads 0 rather than NaN."""
    percentages = run_module("""
      import { assemblyProgress } from './src/music_video_producer/web/assets/api.js';
      const job = (extra) => Object.assign(
        { kind: 'post', prompt_id: '', status: 'running', progress: 0 }, extra);
      console.log(JSON.stringify({
        none: assemblyProgress({ jobs: [] }),
        missing: assemblyProgress({}),
        running: assemblyProgress({ jobs: [job({ progress: 42 })] }),
        legacy: assemblyProgress({ jobs: [{ kind: 'post', prompt_id: '', status: 'running' }] }),
        settled: assemblyProgress({ jobs: [job({ status: 'complete', progress: 100 })] }),
        errored: assemblyProgress({ jobs: [job({ status: 'error', progress: 45 })] }),
        comfy: assemblyProgress({ jobs: [{ kind: 'h3', prompt_id: 'p-1', status: 'running' }] }),
        newest: assemblyProgress({ jobs: [job({ progress: 10 }), job({ progress: 70 })] }),
        overflow: assemblyProgress({ jobs: [job({ progress: 900 })] }),
        garbage: assemblyProgress({ jobs: [job({ progress: 'soon' })] }),
      }));
    """)

    assert percentages["none"] is None
    assert percentages["missing"] is None
    assert percentages["running"] == 42
    assert percentages["legacy"] == 0
    assert percentages["settled"] is None
    assert percentages["errored"] is None
    assert percentages["comfy"] is None
    assert percentages["newest"] == 70
    assert percentages["overflow"] == 100
    assert percentages["garbage"] == 0


def test_the_assembly_bar_draws_the_preset_select_and_the_running_percentage():
    """The bar is drawn entirely by `renderAssembly`, so this is where the control has to
    appear. Read from source, because a browser is not available here: the options come from
    api.js's table rather than a second hand-written list, the chosen preset is what the
    click sends, and the in-flight label carries the number the poll reads back."""
    source = APP_JS.read_text(encoding="utf-8")
    block = source.split("function renderAssembly()", 1)[1].split("\nfunction ", 1)[0]

    assert "EXPORT_PRESETS.map(" in block
    assert 'id="assembly-preset"' in block
    assert "assemblyPreset" in block
    assert "api.assemble(projectId, preset)" in block
    # The percentage rides the running label, and only when one has been read.
    assert "assemblyPercent === null ? ASSEMBLE_RUNNING" in block
    # The poll starts with the click and stops with it: an interval left running after a
    # synchronous request settles would fetch the project every two seconds forever.
    assert "watchAssemblyProgress(projectId);" in block
    assert "stopAssemblyProgress();" in block
    assert "assemblyProgress(fresh)" in source
    assert "clearInterval(assemblyProgressTimer)" in source


# ---------------------------------------------------------------------------------------------
# The appearance anchor (`Asset.consistency_prompt`)
# ---------------------------------------------------------------------------------------------


def consistency_anchor_plans(kinds: list[str]) -> dict:
    """`consistencyAnchorPlan` run for real: every Asset kind, and every draft state."""
    return run_module(f"""
      import {{ CONSISTENCY_PROMPT_LIMIT, consistencyAnchorPlan }}
        from './src/music_video_producer/web/assets/api.js';
      const kinds = {json.dumps(kinds)};
      const offered = {{}};
      for (const kind of kinds) {{
        offered[kind] = consistencyAnchorPlan({{ id: 'a', kind, consistency_prompt: '' }}) !== null;
      }}
      const stored = {{ id: 'a', kind: 'character', consistency_prompt: 'a woman in a red leather jacket' }};
      const bare = {{ id: 'a', kind: 'character' }};
      console.log(JSON.stringify({{
        limit: CONSISTENCY_PROMPT_LIMIT,
        offered,
        nothingSelected: consistencyAnchorPlan(null),
        untouched: consistencyAnchorPlan(stored),
        whitespaceOnly: consistencyAnchorPlan(stored, 'a woman in a red leather jacket   '),
        edited: consistencyAnchorPlan(stored, 'a woman in a black coat'),
        cleared: consistencyAnchorPlan(stored, ''),
        overLong: consistencyAnchorPlan(stored, 'z'.repeat(CONSISTENCY_PROMPT_LIMIT + 1)),
        atLimit: consistencyAnchorPlan(stored, 'z'.repeat(CONSISTENCY_PROMPT_LIMIT)),
        missingField: consistencyAnchorPlan(bare),
      }}));
    """)


def test_the_anchor_editor_decides_every_state_from_one_executed_rule():
    """Executed for every kind an Asset can carry, and for every state of the box.

    The rule is an exclusion rather than a list of blessed kinds, and that is asserted as
    such: a kind added to `AssetKind` later must be offered the field automatically, because
    the failure of a hardcoded list is a feature that is simply missing from the screen while
    every backend test passes. Only `audio` is out, and only because a sound has no
    appearance.

    The bound is read from api.js and compared to the route's, for the reason the song-context
    bound already learned: a client that shortens a paste and a route that refuses the same
    text with a 422 are two rules wearing one number.
    """
    kinds = list(get_args(AssetKind))
    executed = consistency_anchor_plans(kinds)

    assert executed["limit"] == CONSISTENCY_PROMPT_LIMIT
    assert {kind for kind in kinds if executed["offered"][kind]} == set(kinds) - {"audio"}
    assert executed["nothingSelected"] is None

    # A freshly selected asset reports the stored text and offers no save: writing an anchor
    # that is already stored spends a manifest save to change nothing.
    assert executed["untouched"]["draft"] == "a woman in a red leather jacket"
    assert executed["untouched"]["changed"] is False
    assert executed["untouched"]["savable"] is False
    # Trailing whitespace is not an edit, because the route trims before it stores.
    assert executed["whitespaceOnly"]["savable"] is False

    assert executed["edited"]["savable"] is True
    # Emptying the box is a real edit — an anchor that cannot be withdrawn is one the
    # Director cannot correct.
    assert executed["cleared"]["changed"] is True
    assert executed["cleared"]["savable"] is True
    assert executed["cleared"]["length"] == 0

    # Over the bound: counted, said in words rather than only in a colour, and unsavable, so
    # the button cannot send a request the route is certain to refuse.
    assert executed["overLong"]["over"] is True
    assert executed["overLong"]["savable"] is False
    assert "too long to save" in executed["overLong"]["count"]
    assert executed["atLimit"]["over"] is False
    assert executed["atLimit"]["savable"] is True

    # An asset loaded from a manifest written before the field existed carries no key at all.
    assert executed["missingField"]["stored"] == ""
    assert executed["missingField"]["savable"] is False


def test_the_inspector_draws_the_anchor_above_the_generation_prompt_and_saves_it_alone():
    """The render and the click are both executed; nothing here reads app.js as text.

    Three things a source-reading test could not see. That the box is drawn at all and holds
    the *stored* anchor. That it sits above the read-only generation prompt — the anchor
    outranks it everywhere both are consumed, and a panel that puts the machine's text first
    teaches the opposite. And that saving goes through the dedicated route: an anchor folded
    into the whole-project PUT would be silently re-adopted by the server and lost.
    """
    fired = run_workspace("""
      const arrange = (asset) => {
        state.project = { id: 'p1', shots: [], jobs: [], assets: [asset] };
        state.selectedAssetId = asset.id;
        app.renderAssetInspector();
        return at('#asset-inspector').innerHTML;
      };
      const character = arrange({
        id: 'a1', kind: 'character', path: 'out/a.png', name: 'Lucy', source: 'upload',
        prompt: 'a woman in a blue dress, studio lighting',
        consistency_prompt: 'a woman in a red leather jacket',
        created_at: '2026-08-20T00:00:00Z',
      });
      const sound = arrange({
        id: 'a2', kind: 'audio', path: 'out/a.wav', name: 'Room tone', source: 'upload',
        prompt: '', consistency_prompt: '', created_at: '2026-08-20T00:00:00Z',
      });

      // Typing repaints the count and the button without rebuilding the panel.
      arrange({
        id: 'a1', kind: 'character', path: 'out/a.png', name: 'Lucy', source: 'upload',
        prompt: '', consistency_prompt: 'a woman in a red leather jacket',
        created_at: '2026-08-20T00:00:00Z',
      });
      // The initial shut state is in the markup: the stub DOM cannot see an attribute the
      // template wrote, only a property a handler set.
      const before = at('#asset-inspector').innerHTML.includes('id="save-asset-anchor" disabled');
      at('#asset-anchor').value = 'a woman in a black coat';
      await fire('#asset-anchor:input', {});
      const afterTyping = {
        disabled: at('#save-asset-anchor').disabled,
        count: at('#asset-anchor-count').textContent,
      };
      at('#asset-anchor').value = 'z'.repeat(1000);
      await fire('#asset-anchor:input', {});
      const afterOverflow = {
        disabled: at('#save-asset-anchor').disabled,
        count: at('#asset-anchor-count').textContent,
      };

      at('#asset-anchor').value = '  a woman in a black coat  ';
      await fire('#asset-anchor:input', {});
      requests.length = 0;
      await fire('#save-asset-anchor:click', {});
      const saved = requests.map((sent) => ({ path: sent.path, method: sent.method, body: sent.body }));

      console.log(JSON.stringify({
        drawn: character.includes('id="asset-anchor"'),
        holdsStored: character.includes('a woman in a red leather jacket'),
        anchorBeforeGenerationPrompt:
          character.indexOf('id="asset-anchor"') < character.indexOf('Generation prompt'),
        noMaxlength: !character.includes('maxlength'),
        soundHasNoBox: !sound.includes('id="asset-anchor"'),
        before, afterTyping, afterOverflow, saved,
      }));
    """, responses={
        "/api/projects/p1/assets/a1/consistency-prompt": {
            "body": {"id": "p1", "shots": [], "jobs": [], "assets": []},
        },
    })

    assert fired["drawn"] is True
    assert fired["holdsStored"] is True
    assert fired["anchorBeforeGenerationPrompt"] is True
    # No `maxlength`: it truncates an oversized paste silently, which is the defect the
    # song-context boxes already recorded. The count and the route's 422 are the only two
    # things that speak about the bound.
    assert fired["noMaxlength"] is True
    assert fired["soundHasNoBox"] is True

    assert fired["before"] is True
    assert fired["afterTyping"]["disabled"] is False
    assert "too long" not in fired["afterTyping"]["count"]
    assert fired["afterOverflow"]["disabled"] is True
    assert "too long to save" in fired["afterOverflow"]["count"]

    # One request, to the dedicated route, carrying the trimmed anchor and nothing else.
    assert fired["saved"] == [
        {
            "path": "/api/projects/p1/assets/a1/consistency-prompt",
            "method": "PUT",
            "body": json.dumps(
                {"consistency_prompt": "a woman in a black coat"}, separators=(",", ":")
            ),
        }
    ]


# A Shot with every field set to something that is not its default, so a copy that inherits
# anything at all is visible field by field rather than only in the fields a fixture bothered
# to fill. Built from the model, so a field added to `Shot` and left out of this fixture fails
# the partition test next door rather than riding into the copy unnoticed.
RENDERED_SHOT = {
    "id": "shot_source",
    "start": 12.5,
    "duration": 4.25,
    "prompt": "A singer turns toward camera",
    "h3_prompt": "Shot 1: a singer turns toward camera",
    "h3_prompt_map": "Reference map: <Picture 1> is the wolf.",
    "mode": "references",
    "asset_ids": ["asset_wolf"],
    "citations": [{"asset_id": "asset_wolf", "role": "reference", "order": 0}],
    "reference_labels": {"asset_wolf": "the wolf"},
    "singing": "singing",
    "use_song_audio": True,
    "seed": 4242,
    "status": "complete",
    "prompt_id": "comfy_prompt_1",
    "latest_output": "shots/shot_source/take_1.mp4",
    "latest_review": {"model": "vision", "summary": "the wolf, centre frame"},
    "approved_output": "shots/shot_source/take_1.mp4",
    "approved_start": 12.5,
    "approved_duration": 4.25,
    "latest_take_lead": 0.25,
    "latest_take_start": 12.5,
    "latest_take_duration": 4.25,
    "trim_nudge": -0.125,
    "mix_take_audio": True,
    "flagged": True,
    "locked": True,
}


def test_the_monitor_says_when_a_newer_render_is_about_to_displace_the_take_on_screen():
    """The Monitor's honesty, executed — the surface a Director actually watches.

    `monitorState` decided purely from `latest_output`, and `.showing-take` display:none's the
    overlay, which was the Monitor's only text layer. So a re-rendering shot played its previous
    take in sync, framed identically to a settled one, with nothing on screen saying a newer
    render was in flight — while the inspector two panels away said exactly that.

    The take still plays and `latest_output` still points at it: the fix is to state the state,
    never to change it. The settled view is pinned byte for byte, because "say more about the
    in-flight case" must not become "say something different about every case".
    """
    from music_video_producer.app import APPROVE_IN_FLIGHT_REFUSAL

    shots = [
        {"id": "settled", "start": 0, "duration": 4, "latest_output": "shots/a.mp4",
         "status": "complete", "latest_take_lead": 0.25, "trim_nudge": 0.125},
        {"id": "rerendering", "start": 4, "duration": 4, "latest_output": "shots/b.mp4",
         "status": "running", "latest_take_lead": 0.25, "trim_nudge": 0.125},
        {"id": "queued", "start": 8, "duration": 4, "latest_output": "shots/c.mp4",
         "status": "queued", "mix_take_audio": True},
        {"id": "first-render", "start": 12, "duration": 4, "status": "running"},
    ]
    seen = run_module(f"""
      import {{ MONITOR_PREVIOUS_TAKE, TAKE_DISPLACED_BY_RENDER, monitorShowsTake, monitorState }}
        from './src/music_video_producer/web/assets/api.js';
      const project = {{ shots: {json.dumps(shots)} }};
      const settled = monitorState(project, 1.0);
      const displaced = monitorState(project, 5.0);
      console.log(JSON.stringify({{
        sentence: TAKE_DISPLACED_BY_RENDER,
        previousKind: MONITOR_PREVIOUS_TAKE,
        settledJson: JSON.stringify(settled),
        displacedJson: JSON.stringify(displaced),
        queued: monitorState(project, 9.0),
        firstRender: monitorState(project, 13.0),
        shows: [settled, displaced, monitorState(project, 13.0), monitorState(project, 99)]
          .map(monitorShowsTake),
        // Nothing about the shot itself moved: the pointer the finishing stages read is
        // exactly where it was.
        pointer: project.shots[1].latest_output,
        status: project.shots[1].status,
      }}));
    """)

    # The settled view, pinned. Every key, every value, in order.
    assert seen["settledJson"] == (
        '{"kind":"take","shot":{"id":"settled","start":0,"duration":4,'
        '"latest_output":"shots/a.mp4","status":"complete","latest_take_lead":0.25,'
        '"trim_nudge":0.125},"takeTime":1.375,"label":"","muted":true}'
    )
    # The displaced view differs in exactly two of those values: the kind, and the label that
    # was empty because a settled take has nothing to explain. The slice and the mix are the
    # settled arithmetic untouched — the take plays exactly as it did.
    displaced = json.loads(seen["displacedJson"])
    settled = json.loads(seen["settledJson"])
    assert displaced["kind"] == seen["previousKind"] != settled["kind"]
    assert displaced["label"] == seen["sentence"]
    assert settled["label"] == ""
    assert displaced["takeTime"] == settled["takeTime"] == 1.375
    assert displaced["muted"] == settled["muted"] is True
    assert list(displaced) == list(settled)

    # A queued shot is in flight exactly as a running one is, and its accepted audio still plays.
    assert seen["queued"]["kind"] == seen["previousKind"]
    assert seen["queued"]["muted"] is False
    # A first render has no take to displace, so the Monitor says what it always said.
    assert seen["firstRender"]["kind"] == "no-take"
    # Both take kinds keep a picture on screen; nothing else does.
    assert seen["shows"] == [True, True, False, False]
    # The pointer and the status are read, never written.
    assert seen["pointer"] == "shots/b.mp4"
    assert seen["status"] == "running"

    # The server's sentence, in the server's words: the refusal the inspector already shows
    # opens with exactly this, so the Monitor and the Approve button describe one state.
    assert APPROVE_IN_FLIGHT_REFUSAL.format(shot="this shot").startswith(seen["sentence"])

    # And executed through the real wiring: a playhead move repaints the Monitor, so this is
    # what a Director scrubbing the timeline is actually shown.
    painted = run_workspace(f"""
      state.project = {{ id: 'p1', shots: {json.dumps(shots)}, jobs: [], assets: [] }};
      const frame = at('#timeline-monitor');
      const note = at('#monitor-note');
      const clock = at('#master-audio');
      const paint = (seconds) => {{
        clock.currentTime = seconds;
        fire('#master-audio:timeupdate');
        return {{ note: note.textContent, showing: frame.classList.contains('showing-take'),
                  overlay: at('#monitor-overlay').textContent }};
      }};
      console.log(JSON.stringify({{
        settled: paint(1.0),
        displaced: paint(5.0),
        firstRender: paint(13.0),
        gap: paint(99),
        backToSettled: paint(1.0),
      }}));
    """)

    # A settled take: a picture and no note, exactly as before.
    assert painted["settled"] == {"note": "", "showing": True, "overlay": ""}
    # A displaced one: the same picture, and the sentence over it.
    assert painted["displaced"]["showing"] is True
    assert painted["displaced"]["note"] == seen["sentence"]
    # The note is not sticky — it belongs to the shot under the playhead, not to the Monitor.
    assert painted["firstRender"]["note"] == ""
    assert painted["firstRender"]["showing"] is False
    assert painted["gap"]["note"] == ""
    # Back onto a settled take, the note goes with it. (The overlay keeps whatever the gap
    # wrote — it is display:none'd behind the picture, which is the hole this note fills.)
    assert painted["backToSettled"]["note"] == ""
    assert painted["backToSettled"]["showing"] is True

    # The layer exists in the markup, and is hidden when empty rather than drawn blank.
    assert 'id="monitor-note"' in INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    assert ".monitor-note:empty { display: none; }" in styles
    # ...and unlike the overlay, it is not hidden by the class a take on screen carries.
    assert ".timeline-monitor.showing-take .monitor-note" not in styles


def test_the_takes_strip_never_calls_a_displaced_take_current():
    """The strip's rows, executed and read as markup.

    `Current` beside a take a newer render is about to replace is an affirmative wrong claim,
    not a missing signal: it says this take is the shot's answer. The row says `Previous` while
    a render is in flight, and the take that is coming gets a row of its own rather than being
    left to inference.
    """
    jobs = [
        {"id": "job_1", "kind": "h3", "target_id": "shot_a", "status": "complete",
         "output_files": ["shots/shot_a/take_1.mp4", "shots/shot_a/take_1-audio.mp4"]},
        {"id": "job_2", "kind": "h3", "target_id": "shot_a", "status": "complete",
         "output_files": ["shots/shot_a/take_2.mp4"]},
        {"id": "job_3", "kind": "post", "target_id": "shot_a", "status": "complete",
         "output_files": ["exports/cut.mp4"]},
    ]
    rows = run_module(f"""
      import {{ TAKE_CURRENT_CHIP, TAKE_PENDING_CHIP, TAKE_PREVIOUS_CHIP, TAKE_USE_CHIP,
        takesStripRows }} from './src/music_video_producer/web/assets/api.js';
      const jobs = {json.dumps(jobs)};
      const shot = (status) => ({{ id: 'shot_a', status,
        latest_output: 'shots/shot_a/take_2.mp4' }});
      console.log(JSON.stringify({{
        chips: {{ current: TAKE_CURRENT_CHIP, previous: TAKE_PREVIOUS_CHIP,
                  use: TAKE_USE_CHIP, pending: TAKE_PENDING_CHIP }},
        settled: takesStripRows({{ jobs }}, shot('complete')),
        running: takesStripRows({{ jobs }}, shot('running')),
        queued: takesStripRows({{ jobs }}, shot('queued')),
        firstRender: takesStripRows({{ jobs: [] }}, {{ id: 'shot_a', status: 'running' }}),
        nothing: takesStripRows(undefined, undefined),
      }}));
    """)

    chips = rows["chips"]
    # Settled: two takes (the `-audio` sibling is the same take), the pointed-at one marked.
    settled = rows["settled"]["rows"]
    assert [row["chip"] for row in settled] == [chips["use"], chips["current"]]
    assert [row["file"] for row in settled] == [
        "shots/shot_a/take_1.mp4", "shots/shot_a/take_2.mp4",
    ]
    assert [row["disabled"] for row in settled] == [False, True]
    assert rows["settled"]["inFlight"] is False

    # In flight: the same two takes, and the claim withdrawn from the one being displaced.
    for status in ("running", "queued"):
        strip = rows[status]
        chips_seen = [row["chip"] for row in strip["rows"]]
        assert chips["current"] not in chips_seen, status
        assert chips_seen == [chips["use"], chips["previous"], chips["pending"]], status
        # The pending row names no file, because there is no file yet, and cannot be clicked.
        pending = strip["rows"][-1]
        assert pending["pending"] is True and pending["file"] == ""
        assert pending["disabled"] is True
        assert "Take 3" in pending["text"]
        # The displaced row still points at the real take: `Use` on it would be a no-op, and
        # nothing here has moved the shot off it.
        assert strip["rows"][1]["file"] == "shots/shot_a/take_2.mp4"
        assert strip["rows"][1]["current"] is True
        assert strip["rows"][1]["displaced"] is True

    # A first render has no takes at all and still shows the one that is coming.
    assert [row["chip"] for row in rows["firstRender"]["rows"]] == [chips["pending"]]
    assert rows["nothing"] == {"rows": [], "inFlight": False, "takes": 0}

    # And the markup the panel actually writes: the strip is drawn for a single take once a
    # render is in flight, because one take plus the pending row is two rows.
    both = jobs + [
        dict(job, id=f"{job['id']}_b", target_id="shot_b") for job in jobs if job["kind"] == "h3"
    ]
    drawn = run_workspace(f"""
      state.project = {{ id: 'p1', jobs: {json.dumps(both)}, assets: [], shots: [
        {{ id: 'shot_a', start: 0, duration: 4, prompt: 'a wolf', status: 'running',
           latest_output: 'shots/shot_a/take_2.mp4' }},
        {{ id: 'shot_b', start: 4, duration: 4, prompt: 'a wolf', status: 'complete',
           latest_output: 'shots/shot_a/take_2.mp4' }},
      ] }};
      const html = (id) => {{
        state.selectedShotId = id;
        app.renderShotInspector();
        return at('#shot-inspector').innerHTML;
      }};
      console.log(JSON.stringify({{ inFlight: html('shot_a'), settled: html('shot_b') }}));
    """)

    assert ">Current<" in drawn["settled"]
    assert ">Previous<" not in drawn["settled"]
    assert ">Current<" not in drawn["inFlight"]
    assert ">Previous<" in drawn["inFlight"]
    assert ">Rendering<" in drawn["inFlight"]
    assert "not landed yet" in drawn["inFlight"]


def test_the_timeline_clip_states_a_render_in_flight_in_words_and_in_its_accessible_name():
    """The clip's render state, executed through `renderTimeline` and read as markup.

    The clip carried render state as a left-border hue and nothing else — no word, and nothing
    in `aria-label`, which is the only one of a clip's signals a screen reader announces. This
    stylesheet's own rule is that colour is never the only signal; the `NO PROMPT` flag is the
    precedent for how a clip states something in words.
    """
    shots = [
        {"id": "shot_a", "start": 0, "duration": 4, "prompt": "a wolf at the window",
         "status": "running", "latest_output": "shots/a.mp4"},
        {"id": "shot_b", "start": 4, "duration": 4, "prompt": "a wolf in the snow",
         "status": "complete", "latest_output": "shots/b.mp4"},
        {"id": "shot_c", "start": 8, "duration": 4, "prompt": "", "status": "queued"},
    ]
    drawn = run_workspace(f"""
      import {{ SHOT_RENDERING_FLAG, TAKE_DISPLACED_BY_RENDER, RENDER_IN_FLIGHT_NO_TAKE }}
        from './src/music_video_producer/web/assets/api.js';
      state.project = {{ id: 'p1', shots: {json.dumps(shots)}, jobs: [], assets: [] }};
      state.selectedShotId = 'shot_b';
      // The zoom control is the shortest path to a real `renderTimeline` from outside it.
      fire('#zoom-in:click');
      const track = at('#shots-track').innerHTML;
      // One clip's whole markup, by its shot id. Clips nest no divs, so the first close ends it.
      const clips = track.split('<div class="shot-clip').slice(1)
        .map((part) => '<div class="shot-clip' + part.split('</div>')[0]);
      const clip = (id) => clips.find((html) => html.includes('data-shot-id="' + id + '"'));
      console.log(JSON.stringify({{
        flag: SHOT_RENDERING_FLAG,
        displacedSentence: TAKE_DISPLACED_BY_RENDER,
        firstRenderSentence: RENDER_IN_FLIGHT_NO_TAKE,
        rerendering: clip('shot_a'),
        settled: clip('shot_b'),
        firstRender: clip('shot_c'),
      }}));
    """)

    # The word, on the clip, for both shapes of in-flight.
    assert f'<span class="clip-state">{drawn["flag"]}</span>' in drawn["rerendering"]
    assert f'<span class="clip-state">{drawn["flag"]}</span>' in drawn["firstRender"]
    assert "clip-state" not in drawn["settled"]

    # And the sentence in the accessible name, which is where the state exists at all for a
    # Director who never sees the border.
    assert f'aria-label="{drawn["displacedSentence"]}' not in drawn["settled"]
    for name in ("aria-label", "title"):
        assert f'{name}="a wolf at the window {drawn["displacedSentence"]}"' in drawn["rerendering"]
        assert f'{name}="a wolf in the snow"' in drawn["settled"]
    # A shot with nothing to displace says the fact without naming a displacement that is not
    # happening — and keeps the NO PROMPT diagnosis it already had.
    assert drawn["firstRenderSentence"] in drawn["firstRender"]
    assert drawn["displacedSentence"] not in drawn["firstRender"]
    assert "NO PROMPT" in drawn["firstRender"]

    # Colour is the second signal, never the only one: the hue rules stay, and the word is what
    # survives them being ignored.
    styles = STYLES_CSS.read_text(encoding="utf-8")
    assert ".shot-clip .clip-state" in styles
    assert ".shot-clip.status-queued, .shot-clip.status-running" in styles


def test_a_new_shot_made_from_another_carries_the_plan_and_no_take():
    """Duplicate and Split, executed, then read field by field off the copy.

    Both handlers cloned the whole Shot and reset `status`, so the copy owned the original's
    take: it played that take in the Monitor, offered it in the takes strip, and read as
    approved. Asserted per field rather than in bulk, so a field added to `Shot` and classified
    by nobody fails here instead of riding into every copy from then on.
    """
    made = run_workspace(f"""
      state.project = {{ id: 'p1', jobs: [], assets: [], shots: [{json.dumps(RENDERED_SHOT)}] }};
      state.selectedShotId = 'shot_source';
      fire('#duplicate-shot:click');
      const duplicate = state.project.shots[1];
      state.selectedShotId = 'shot_source';
      fire('#split-shot:click');
      const half = state.project.shots[2];
      console.log(JSON.stringify({{
        duplicate,
        half,
        source: state.project.shots[0],
        ids: state.project.shots.map((shot) => shot.id),
      }}));
    """)

    source = RENDERED_SHOT
    # The fixture has to differ from the model's defaults in every field a copy must not carry,
    # or "the copy holds the default" would pass on a copy that inherited everything.
    for field in SHOT_TAKE_PROVENANCE_FIELDS | SHOT_UNINHERITED_DECISION_FIELDS:
        default = Shot.model_fields[field].get_default(call_default_factory=True)
        assert source[field] != default, f"the fixture leaves {field} at its default"

    for made_shot, name in ((made["duplicate"], "duplicate"), (made["half"], "split half")):
        # A new identity, never the original's.
        assert made_shot["id"] != source["id"], name

        # Plan content: present, and equal to the original's, field by field.
        for field in sorted(SHOT_PLAN_CONTENT_FIELDS - {"start", "duration"}):
            assert field in made_shot, f"{name} lost plan field {field}"
            assert made_shot[field] == source[field], f"{name} changed plan field {field}"

        # Take provenance: absent, or the model's own default and nothing else.
        for field in sorted(SHOT_TAKE_PROVENANCE_FIELDS | SHOT_UNINHERITED_DECISION_FIELDS):
            default = Shot.model_fields[field].get_default(call_default_factory=True)
            assert made_shot.get(field, default) == default, (
                f"{name} inherited {field} from a take it never rendered"
            )

        # The copy is a Shot the server accepts, with the model's own defaults filling the rest.
        loaded = Shot.model_validate(made_shot)
        for field in SHOT_TAKE_PROVENANCE_FIELDS | SHOT_UNINHERITED_DECISION_FIELDS:
            assert getattr(loaded, field) == Shot.model_fields[field].get_default(
                call_default_factory=True
            ), f"{name}.{field}"

    # The windows: the duplicate follows the original, the split halves it in place.
    assert made["duplicate"]["start"] == source["start"] + source["duration"]
    assert made["duplicate"]["duration"] == source["duration"]
    assert made["half"]["start"] == source["start"] + source["duration"] / 2
    assert made["half"]["duration"] == source["duration"] / 2

    # And the original is untouched but for the window the split narrowed: its take, its
    # approval and its pointer all stay exactly where they were. Nothing in this workspace
    # clears `latest_output` or `approved_output`, and this is the assertion that says so.
    for field in SHOT_TAKE_PROVENANCE_FIELDS | SHOT_UNINHERITED_DECISION_FIELDS:
        assert made["source"][field] == source[field], field
    assert made["source"]["latest_output"] == source["latest_output"]
    assert made["source"]["approved_output"] == source["approved_output"]
    assert made["source"]["duration"] == source["duration"] / 2
    assert len(set(made["ids"])) == 3


def test_the_client_and_server_agree_on_what_a_new_shot_inherits():
    """One classification, partitioned against the model itself.

    The client builds a copy from `SHOT_PLAN_CONTENT_FIELDS`, so a field the model gains and
    nobody classifies is simply absent from every copy — which is the safe direction, and a
    silent one. This is what makes it loud: the three sets must cover `Shot` exactly, and the
    client's list must be the server's, so an unclassified field fails the suite.
    """
    listed = run_module("""
      import { SHOT_PLAN_CONTENT_FIELDS, NEW_SHOT_STATUS }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({ plan: SHOT_PLAN_CONTENT_FIELDS, status: NEW_SHOT_STATUS }));
    """)

    assert set(listed["plan"]) == set(SHOT_PLAN_CONTENT_FIELDS)
    assert len(listed["plan"]) == len(SHOT_PLAN_CONTENT_FIELDS), "the client's list repeats a field"

    # The partition, against the model and not against a copy of its field names.
    classified = (
        SHOT_PLAN_CONTENT_FIELDS | SHOT_TAKE_PROVENANCE_FIELDS | SHOT_UNINHERITED_DECISION_FIELDS
    )
    assert classified | {"id"} == set(Shot.model_fields), (
        "a Shot field is classified by nobody — say whether a new Shot inherits it"
    )
    assert not SHOT_PLAN_CONTENT_FIELDS & SHOT_TAKE_PROVENANCE_FIELDS
    assert not SHOT_PLAN_CONTENT_FIELDS & SHOT_UNINHERITED_DECISION_FIELDS
    assert not SHOT_TAKE_PROVENANCE_FIELDS & SHOT_UNINHERITED_DECISION_FIELDS

    # The status a Shot nobody has rendered carries is the model's own default, written out
    # loud because the inspector draws its chip from the field.
    assert listed["status"] == Shot.model_fields["status"].get_default()

    # No copy path clones a Shot any more: `structuredClone(shot)` is what carried the take.
    #
    # Read to the handler's own closing `  });` rather than to the end of its first line. The
    # split handler stopped being a one-liner on 2026-08-21, when it gained the refusal it used to
    # decline windows under a second in silence — and a source assertion that depends on where the
    # newlines fall is an assertion about formatting, not about what the copy carries.
    workspace = APP_JS.read_text(encoding="utf-8")
    for handler in ("#duplicate-shot", "#split-shot"):
        chunk = workspace.split(f'$("{handler}").addEventListener', 1)[1]
        first, _, rest = chunk.partition("\n")
        # A one-liner closes on its own line; a block closes at the next `  });` in column two.
        body = first
        if not first.rstrip().endswith("});"):
            body = "\n".join([first, rest.split("\n  });", 1)[0]])
        assert "newShotFromPlan(shot" in body, handler
        assert "structuredClone(shot)" not in body, handler


# ------------------------------------------------------------------------------------------
# Snap cuts to phrase boundaries: the browser half. `snapCutsControl` and `snapCutsReportLines`
# are executed under node, and `renderSnapCuts` is *run* against the stub DOM with its markup
# read afterwards — never grepped for identifiers. A substring assertion over `app.js` is what
# let three UI guarantees invert with a green suite once already.
# ------------------------------------------------------------------------------------------

SNAP_PROJECT = {
    "id": "p1",
    "jobs": [],
    "song": {
        "title": "Measured",
        "source": "imported",
        "path": "media/songs/000-master.wav",
        "duration": 24,
        "vocal_spans": [[0.5, 7.0], [8.0, 13.0], [14.0, 19.5]],
    },
    "shots": [
        {"id": "s0", "start": 0, "duration": 6},
        {"id": "s1", "start": 6, "duration": 6},
        {"id": "s2", "start": 12, "duration": 12},
    ],
}

SNAP_REPORT = {
    "applied": False,
    "status": "ready",
    "tolerance": 1.5,
    "moved": 2,
    "skipped": 1,
    "moves": [
        {"before": "SHOT 01 (s0)", "after": "SHOT 02 (s1)",
         "boundary": 6.0, "proposed": 7.15, "shift": 1.15},
        {"before": "SHOT 02 (s1)", "after": "SHOT 03 (s2)",
         "boundary": 12.0, "proposed": 13.15, "shift": 1.15},
    ],
    "skips": [
        {"before": "SHOT 03 (s2)", "after": "SHOT 04 (s3)", "boundary": 18.0,
         "reason": "SHOT 03 (s2) is locked. A lock is a deliberate hands-off on this shot."},
    ],
    "message": "",
    "project": None,
}


def test_the_snap_cuts_control_decides_its_own_refusals_and_its_two_stages():
    """Executed, not read: every branch of `snapCutsControl` over one table of projects.

    The cheap facts are the browser's — a song, a measurement, two shots, a non-zero tolerance
    — and the two-stage half is the one that matters: with a report holding moves in hand the
    same button becomes the apply, which is what makes the confirm step unskippable in the
    interface as well as on the wire.
    """
    decisions = run_module("""
      import { snapCutsControl, SNAP_CUTS_NO_SONG, SNAP_CUTS_UNMEASURED,
               SNAP_CUTS_WITHOUT_CUTS, SNAP_CUTS_TOLERANCE_OFF, SNAP_CUTS_LABEL,
               SNAP_CUTS_HELP, SNAP_CUTS_NOTHING_TO_MOVE }
        from './src/music_video_producer/web/assets/api.js';
      const base = __PROJECT__;
      const report = __REPORT__;
      const clone = (extra) => JSON.parse(JSON.stringify({ ...base, ...extra }));
      const songless = clone({}); songless.song = null;
      const unheard = clone({}); unheard.song.vocal_spans = [];
      const oneShot = clone({}); oneShot.shots = [base.shots[0]];
      console.log(JSON.stringify({
        songless: snapCutsControl(songless, 1.5),
        unheard: snapCutsControl(unheard, 1.5),
        oneShot: snapCutsControl(oneShot, 1.5),
        off: snapCutsControl(clone({}), 0),
        offByString: snapCutsControl(clone({}), "0"),
        ready: snapCutsControl(clone({}), 1.5),
        withReport: snapCutsControl(clone({}), 1.5, report),
        emptyReport: snapCutsControl(clone({}), 1.5,
          { ...report, moves: [], moved: 0 }),
        wording: { SNAP_CUTS_NO_SONG, SNAP_CUTS_UNMEASURED, SNAP_CUTS_WITHOUT_CUTS,
                   SNAP_CUTS_TOLERANCE_OFF, SNAP_CUTS_LABEL, SNAP_CUTS_HELP,
                   SNAP_CUTS_NOTHING_TO_MOVE },
      }));
    """.replace("__PROJECT__", json.dumps(SNAP_PROJECT)).replace("__REPORT__", json.dumps(SNAP_REPORT)))

    wording = decisions["wording"]
    for case, reason in (
        ("songless", "SNAP_CUTS_NO_SONG"),
        ("unheard", "SNAP_CUTS_UNMEASURED"),
        ("oneShot", "SNAP_CUTS_WITHOUT_CUTS"),
        ("off", "SNAP_CUTS_TOLERANCE_OFF"),
        ("offByString", "SNAP_CUTS_TOLERANCE_OFF"),
    ):
        assert decisions[case]["disabled"] is True, case
        assert decisions[case]["apply"] is False, case
        assert decisions[case]["reason"] == wording[reason], case
    # Runnable, and reporting rather than applying until a report exists.
    assert decisions["ready"] == {
        "disabled": False, "apply": False,
        "label": wording["SNAP_CUTS_LABEL"], "title": wording["SNAP_CUTS_HELP"], "reason": "",
    }
    assert decisions["withReport"]["apply"] is True
    assert decisions["withReport"]["label"] == "Apply 2 move(s)"
    assert decisions["withReport"]["reason"] == "2 cut(s) would move, 1 would stay."
    # A report with nothing in it does not turn the button into an apply, and says so.
    assert decisions["emptyReport"]["apply"] is False
    assert decisions["emptyReport"]["label"] == wording["SNAP_CUTS_LABEL"]
    assert decisions["emptyReport"]["reason"] == wording["SNAP_CUTS_NOTHING_TO_MOVE"]


def test_the_snap_report_lines_carry_every_move_and_every_skip_reason_verbatim():
    """The report is the feature's other half, so nothing in it is summarised or dropped.

    A skip's line is the **server's own sentence**, unedited: the refusals are decided once, in
    Python, and a client that paraphrased one would be a second opinion that can drift from the
    one that actually stops the write.
    """
    lines = run_module("""
      import { snapCutsReportLines } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        lines: snapCutsReportLines(__REPORT__),
        empty: snapCutsReportLines(null),
      }));
    """.replace("__REPORT__", json.dumps(SNAP_REPORT)))

    assert lines["empty"] == []
    assert [line["kind"] for line in lines["lines"]] == ["move", "move", "skip"]
    assert lines["lines"][0]["text"] == (
        "SHOT 01 (s0) → SHOT 02 (s1): 6.000s → 7.150s (+1.150s)"
    )
    assert lines["lines"][2]["text"] == SNAP_REPORT["skips"][0]["reason"]
    assert len(lines["lines"]) == len(SNAP_REPORT["moves"]) + len(SNAP_REPORT["skips"])


def test_a_move_line_says_how_long_the_gap_it_found_was():
    """The Director's framing, 2026-08-20, drawn where the move already is.

    "A 1 second gap may just be an extended shot where a 4 second gap would be great for a
    b-roll or non singing character shot." Two moves of the *same* distance land in gaps of
    very different sizes, and without the length the two lines are identical — so the clause
    is the only thing separating an extended shot from a scene worth planning. It suggests
    nothing; the number is the whole addition. A report with no `gap` (one from a server older
    than the field) loses the clause rather than drawing `NaNs`.
    """
    report = json.loads(json.dumps(SNAP_REPORT))
    report["moves"][0]["gap"] = 1.0
    report["moves"][1]["gap"] = 4.4

    lines = run_module("""
      import { snapCutsReportLines } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        lines: snapCutsReportLines(__REPORT__),
        gapless: snapCutsReportLines(__GAPLESS__),
      }));
    """.replace("__REPORT__", json.dumps(report)).replace("__GAPLESS__", json.dumps(SNAP_REPORT)))

    assert lines["lines"][0]["text"] == (
        "SHOT 01 (s0) → SHOT 02 (s1): 6.000s → 7.150s (+1.150s) in a 1.000s gap"
    )
    assert lines["lines"][1]["text"].endswith("in a 4.400s gap")
    # The skip lines are still the server's own sentences, untouched by any of this.
    assert lines["lines"][2]["text"] == SNAP_REPORT["skips"][0]["reason"]
    assert "gap" not in lines["gapless"][0]["text"]


def test_a_move_line_says_when_the_number_it_names_is_the_centre_of_a_transition():
    """R-3's consequence for the report: an overlapping seam has no edge at the cut.

    A transition is authored as an overlap, and `timeline.SEAM_POINT` places its *midpoint* in
    the silence — an instant at which neither clip begins or ends. A Director reading
    "144.268s" and looking for a clip edge there would find nothing, so the line says what the
    number is and that the blend moved whole rather than being trimmed to fit. A hard cut
    carries `overlap` 0 and reads exactly as it always has, which is what keeps the clause a
    statement about transitions rather than decoration on every line.
    """
    report = json.loads(json.dumps(SNAP_REPORT))
    report["moves"][0]["overlap"] = 5.492
    report["moves"][1]["overlap"] = 0

    lines = run_module("""
      import { snapCutsReportLines } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        lines: snapCutsReportLines(__REPORT__),
        older: snapCutsReportLines(__OLDER__),
      }));
    """.replace("__REPORT__", json.dumps(report)).replace("__OLDER__", json.dumps(SNAP_REPORT)))

    assert lines["lines"][0]["text"].endswith("· centre of a 5.492s overlap, moved whole")
    assert "overlap" not in lines["lines"][1]["text"], "a hard cut reads as it always has"
    # A report from a server older than the field loses the clause rather than drawing `NaN`.
    assert "overlap" not in lines["older"][0]["text"]


def test_the_snap_button_runs_for_a_song_measured_in_words_alone():
    """`timeline.vocal_gaps` reads `lyric_words` first, so the browser's gate must too.

    A song with words and no merged spans is measured — cut placement reads exactly those
    words — and a button drawn shut on the spans alone would refuse a plan the server would
    happily make. Neither measurement is still unmeasured, which is the rule that has to
    survive: two empty lists are two absences, not a silent track.
    """
    decisions = run_module("""
      import { snapCutsControl, SNAP_CUTS_UNMEASURED }
        from './src/music_video_producer/web/assets/api.js';
      const base = __PROJECT__;
      const clone = () => JSON.parse(JSON.stringify(base));
      const wordsOnly = clone();
      wordsOnly.song.vocal_spans = [];
      wordsOnly.song.lyric_words = [["I", 0.5, 0.9], ["sing", 0.9, 1.4]];
      const neither = clone();
      neither.song.vocal_spans = [];
      neither.song.lyric_words = [];
      console.log(JSON.stringify({
        wordsOnly: snapCutsControl(wordsOnly, 1.5),
        neither: snapCutsControl(neither, 1.5),
        unmeasured: SNAP_CUTS_UNMEASURED,
      }));
    """.replace("__PROJECT__", json.dumps(SNAP_PROJECT)))

    assert decisions["wordsOnly"]["disabled"] is False
    assert decisions["neither"]["disabled"] is True
    assert decisions["neither"]["reason"] == decisions["unmeasured"]


def test_the_snap_bar_draws_the_whole_report_and_only_then_offers_to_apply():
    """The two-stage control, executed end to end against the stub DOM and read as markup.

    The first click sends a report request with `confirm_apply` false; the bar then holds every
    move line and every skip reason, and the button has become the apply. The second click is
    the one that carries the flag. Read out of the rendered markup rather than grepped for in
    `app.js`, because the recorded incident is exactly that: substring assertions over `app.js`
    let three UI guarantees invert with a green suite.
    """
    run = run_workspace(
        """
      state.project = __PROJECT__;
      app.renderSnapCuts();
      const before = at('#snap-bar').innerHTML;
      requests.length = 0;
      await fire('#snap-cuts:click');
      await flush();
      app.renderSnapCuts();
      const reported = at('#snap-bar').innerHTML;
      const firstRequest = requests[0];
      requests.length = 0;
      await fire('#snap-cuts:click');
      await flush();
      console.log(JSON.stringify({
        before, reported,
        firstRequest,
        secondRequest: requests[0],
      }));
    """.replace("__PROJECT__", json.dumps(SNAP_PROJECT)),
        responses={"/api/projects/p1/timeline/snap-cuts": {"body": SNAP_REPORT}},
    )

    # Stage one: the button reports, and nothing about applying is on screen yet.
    assert 'id="snap-cuts"' in run["before"]
    assert ">Snap cuts<" in run["before"]
    assert "snap-report" not in run["before"]
    assert "Apply" not in run["before"]
    assert json.loads(run["firstRequest"]["body"]) == {"tolerance": 0.75, "confirm_apply": False}
    assert run["firstRequest"]["method"] == "POST"
    assert run["firstRequest"]["path"] == "/api/projects/p1/timeline/snap-cuts"

    # Stage two: the whole report is on screen -- both headings, every move, every skip reason.
    assert "snap-report" in run["reported"]
    assert "Would move (2)" in run["reported"]
    assert "Would stay (1)" in run["reported"]
    assert "SHOT 01 (s0) → SHOT 02 (s1): 6.000s → 7.150s (+1.150s)" in run["reported"]
    assert "SHOT 02 (s1) → SHOT 03 (s2): 12.000s → 13.150s (+1.150s)" in run["reported"]
    assert SNAP_REPORT["skips"][0]["reason"] in run["reported"]
    assert ">Apply 2 move(s)<" in run["reported"]
    assert "2 cut(s) would move, 1 would stay." in run["reported"]
    # And a way out that is not an apply.
    assert 'id="snap-dismiss"' in run["reported"]
    # Only the second click carries the confirmation.
    assert json.loads(run["secondRequest"]["body"]) == {"tolerance": 0.75, "confirm_apply": True}


def test_the_snap_tolerance_box_is_drawn_with_the_bounds_the_request_schema_enforces():
    """A box offering a tolerance the route refuses is a control whose only outcome is a 422."""
    markup = run_workspace("""
      state.project = __PROJECT__;
      app.renderSnapCuts();
      console.log(JSON.stringify({ bar: at('#snap-bar').innerHTML }));
    """.replace("__PROJECT__", json.dumps(SNAP_PROJECT)))["bar"]

    box = re.search(r'<input type="number" id="snap-tolerance"[^>]*>', markup)
    assert box, markup
    assert 'min="0"' in box.group(0)
    assert f'max="{SNAP_TOLERANCE_MAX:g}"' in box.group(0)
    assert f'value="{SNAP_TOLERANCE_DEFAULT:g}"' in box.group(0)


def test_the_snap_tolerance_constants_are_the_servers_own():
    """Held together rather than transcribed: `SnapCutsRequest` bounds the field with these two
    numbers, and the browser draws its box from them."""
    constants = run_module("""
      import { SNAP_TOLERANCE_DEFAULT, SNAP_TOLERANCE_MAX, snapTolerance }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        default: SNAP_TOLERANCE_DEFAULT,
        max: SNAP_TOLERANCE_MAX,
        clamped: [snapTolerance('9'), snapTolerance('-2'), snapTolerance(''),
                  snapTolerance('0.4'), snapTolerance('abc')],
      }));
    """)

    assert constants["default"] == SNAP_TOLERANCE_DEFAULT
    assert constants["max"] == SNAP_TOLERANCE_MAX
    # An unusable box answers the default rather than an empty key: the request needs a number.
    assert constants["clamped"] == [
        SNAP_TOLERANCE_MAX, 0, SNAP_TOLERANCE_DEFAULT, 0.4, SNAP_TOLERANCE_DEFAULT
    ]
    fields = SnapCutsRequest.model_fields["tolerance"]
    bounds = {type(item).__name__: getattr(item, "ge", getattr(item, "le", None))
              for item in fields.metadata}
    assert fields.default == SNAP_TOLERANCE_DEFAULT
    assert bounds == {"Ge": 0, "Le": SNAP_TOLERANCE_MAX}


def test_the_snap_cuts_client_calls_a_route_the_application_serves():
    """The path is built in one place in `api.js` and served in one place in `app.py`; a typo in
    either is a button whose only outcome is a 404."""
    call = run_module("""
      import { api } from './src/music_video_producer/web/assets/api.js';
      let seen = { path: '', method: '', body: '' };
      globalThis.fetch = (path, options = {}) => {
        seen = { path, method: options.method || 'GET', body: options.body || '' };
        return Promise.reject(new Error('the contract harness makes no requests'));
      };
      await api.snapCuts('PID', 1.5, true).catch(() => {});
      console.log(JSON.stringify(seen));
    """)

    assert call["path"] == "/api/projects/PID/timeline/snap-cuts"
    assert call["method"] == "POST"
    assert json.loads(call["body"]) == {"tolerance": 1.5, "confirm_apply": True}
    assert "/api/projects/{project_id}/timeline/snap-cuts" in {
        route.path for route in create_app().routes
    }


# --------------------------------------------------------------------------------------------
# Live render progress on the two surfaces the Director named: the asset card and the clip.
#
# Every one of these executes the code. The recorded incident this file exists around is three
# UI guarantees inverting under a green suite of substring assertions over app.js source, so the
# render functions are run against the stub DOM and the markup they produced is read back.
# --------------------------------------------------------------------------------------------

#: One render genuinely mid-flight, with a percentage on it, in the route's fixed shape.
POLL_IN_FLIGHT = {
    "active": True,
    "comfy_online": True,
    "jobs": [{
        "id": "j1", "kind": "flux", "status": "running", "prompt_id": "pr1",
        "target_id": "a1", "seed": 7, "output_files": [], "error": "",
    }],
    "shots": [],
    "assets": [{"asset_id": "a1", "path": ""}],
    "song": None,
    "progress": [{"job_id": "j1", "prompt_id": "pr1", "percent": 42}],
}


def test_the_progress_helpers_say_nothing_at_all_when_nothing_is_known():
    """Unknown is not zero, and neither is ever invented.

    `null`, an absent key, an empty string, a `NaN` — every one of them is "nobody has said
    anything", and every one draws the plain RENDERING word and an unchanged accessible name.
    A real `0` is the different statement that the render started and no step is done, and it
    is drawn as `0%`. A number that arrived out of range is clamped rather than shown raw."""
    answers = run_module("""
      import { renderProgressLabel, renderingFlag, renderProgressNote, SHOT_RENDERING_FLAG }
        from './src/music_video_producer/web/assets/api.js';
      const cases = [null, undefined, '', 'forty', NaN, Infinity, 0, 42, 42.6, -5, 140, '77'];
      console.log(JSON.stringify({
        word: SHOT_RENDERING_FLAG,
        labels: cases.map((value) => renderProgressLabel(value)),
        flags: cases.map((value) => renderingFlag(value)),
        notes: cases.map((value) => renderProgressNote(value)),
      }));
    """)

    assert answers["word"] == "RENDERING"
    assert answers["labels"] == [
        "", "", "", "", "", "", "0%", "42%", "43%", "0%", "100%", "77%",
    ]
    # Composed with the word, never in place of it -- so a socketless render still says RENDERING.
    assert answers["flags"][:6] == ["RENDERING"] * 6
    assert answers["flags"][6:] == [
        "RENDERING 0%", "RENDERING 42%", "RENDERING 43%",
        "RENDERING 0%", "RENDERING 100%", "RENDERING 77%",
    ]
    assert answers["notes"][:6] == [""] * 6
    assert answers["notes"][6] == "0% of this render is done."
    assert answers["notes"][7] == "42% of this render is done."


def test_progress_is_joined_to_its_target_by_job_and_never_crosses_between_two_renders():
    """A batch of H3 renders is the normal case: two shots rendering at once must not read each
    other's number. The join is `report.progress` (keyed by job) to `report.jobs` (which carry
    the target), so a row naming a job the report does not list is dropped rather than guessed."""
    mapped = run_module("""
      import { renderProgressByTarget } from './src/music_video_producer/web/assets/api.js';
      const report = {
        jobs: [
          { id: 'j1', kind: 'h3', target_id: 'shot_a' },
          { id: 'j2', kind: 'h3', target_id: 'shot_b' },
          { id: 'j3', kind: 'flux', target_id: 'asset_x' },
          { id: 'j4', kind: 'post', target_id: '' },
        ],
        progress: [
          { job_id: 'j1', prompt_id: 'p1', percent: 30 },
          { job_id: 'j2', prompt_id: 'p2', percent: 90 },
          { job_id: 'j4', prompt_id: '', percent: 50 },
          { job_id: 'nobody', prompt_id: 'p9', percent: 70 },
          { job_id: 'j3', prompt_id: 'p3', percent: 'not a number' },
        ],
      };
      console.log(JSON.stringify({
        mapped: renderProgressByTarget(report),
        empty: renderProgressByTarget({ jobs: [], progress: [] }),
        missing: renderProgressByTarget(undefined),
        noProgressKey: renderProgressByTarget({ jobs: [{ id: 'j1', target_id: 'shot_a' }] }),
      }));
    """)

    assert mapped["mapped"] == {"shot_a": 30, "shot_b": 90}
    assert mapped["empty"] == {}
    assert mapped["missing"] == {}
    assert mapped["noProgressKey"] == {}


def test_the_asset_card_states_its_percentage_in_words_beside_the_rendering_flag():
    """The Director's ask on the asset card, executed: RENDERING, and how far through it is.

    Read as markup rather than asserted against a source substring. The percentage is text --
    not a hue, not a bar -- because this stylesheet's rule is that colour is never the only
    signal, and a signal that is only a shape is the same failure in another costume."""
    drawn = run_workspace("""
      state.project = __PROJECT__;
      await flush();
      await app.pollRenderStatus();
      await flush();
      console.log(JSON.stringify({
        grid: at('#asset-grid').innerHTML,
        held: state.renderProgress,
      }));
    """.replace("__PROJECT__", poll_project()),
        responses={"/api/projects/p1/render-status": {"body": POLL_IN_FLIGHT}},
    )

    assert drawn["held"] == {"a1": 42}
    assert "RENDERING 42%" in drawn["grid"]
    # The card still says RENDERING, and still has no image: the percentage composes with the
    # state, it does not replace it.
    assert "<img src=" not in drawn["grid"]
    assert "NO PREVIEW" not in drawn["grid"]


def test_a_percentage_that_stops_arriving_leaves_the_card_exactly_as_it_was_before():
    """The degradation contract on the card, driven through the moment it degrades.

    The socket dropped, or ComfyUI restarted, or the answer simply stopped carrying a number.
    The card falls back to the plain RENDERING word -- the same markup it has always drawn --
    rather than freezing the last percentage it saw, which would be a claim about a render
    nobody is measuring any more."""
    drawn = run_workspace("""
      state.project = __PROJECT__;
      await flush();
      await app.pollRenderStatus();
      await flush();
      const measured = at('#asset-grid').innerHTML;

      responses.set('/api/projects/p1/render-status', { body: __SILENT__ });
      await app.pollRenderStatus();
      await flush();
      console.log(JSON.stringify({
        measured,
        silent: at('#asset-grid').innerHTML,
        held: state.renderProgress,
      }));
    """.replace("__PROJECT__", poll_project())
       .replace("__SILENT__", json.dumps({**POLL_IN_FLIGHT, "progress": []})),
        responses={"/api/projects/p1/render-status": {"body": POLL_IN_FLIGHT}},
    )

    assert '<div class="asset-thumb">RENDERING 42%</div>' in drawn["measured"]
    assert drawn["held"] == {}
    assert '<div class="asset-thumb">RENDERING</div>' in drawn["silent"]
    assert "%" not in drawn["silent"]


def test_the_timeline_clip_composes_the_percentage_with_the_rendering_word_and_the_name():
    """The Director's ask on the timeline Shot box, executed through `renderTimeline`.

    Composed with what today's clip already carries, never in place of it: the RENDERING word
    stays, the displacement sentence stays, and the percentage joins both the visible flag and
    the accessible name -- which is the only one of the clip's signals a screen reader announces,
    so a percentage that lived only in the coloured span would not exist for a Director reading
    it that way. A shot with no percentage is drawn exactly as it was."""
    shots = [
        {"id": "shot_a", "start": 0, "duration": 4, "prompt": "a wolf at the window",
         "status": "running", "latest_output": "shots/a.mp4"},
        {"id": "shot_b", "start": 4, "duration": 4, "prompt": "a wolf in the snow",
         "status": "running"},
        {"id": "shot_c", "start": 8, "duration": 4, "prompt": "a wolf in the dark",
         "status": "complete", "latest_output": "shots/c.mp4"},
    ]
    drawn = run_workspace(f"""
      import {{ TAKE_DISPLACED_BY_RENDER }} from './src/music_video_producer/web/assets/api.js';
      state.project = {{ id: 'p1', shots: {json.dumps(shots)}, jobs: [], assets: [] }};
      // Exactly what a poll tick leaves behind: shot_a measured, shot_b unmeasured, shot_c settled.
      state.renderProgress = {{ shot_a: 42, shot_c: 99 }};
      fire('#zoom-in:click');
      const track = at('#shots-track').innerHTML;
      const clips = track.split('<div class="shot-clip').slice(1)
        .map((part) => '<div class="shot-clip' + part.split('</div>')[0]);
      const clip = (id) => clips.find((html) => html.includes('data-shot-id="' + id + '"'));
      console.log(JSON.stringify({{
        displaced: TAKE_DISPLACED_BY_RENDER,
        measured: clip('shot_a'),
        unmeasured: clip('shot_b'),
        settled: clip('shot_c'),
      }}));
    """)

    # The word and the number, in text, on the clip.
    assert '<span class="clip-state">RENDERING 42%</span>' in drawn["measured"]
    # And in the accessible name, after the sentence the clip already carried.
    for name in ("aria-label", "title"):
        assert (
            f'{name}="a wolf at the window {drawn["displaced"]} '
            f'42% of this render is done."'
        ) in drawn["measured"]

    # Unmeasured: the word alone, and a name with nothing appended -- today's clip, unchanged.
    assert '<span class="clip-state">RENDERING</span>' in drawn["unmeasured"]
    assert "%" not in drawn["unmeasured"]

    # Settled: no state span at all, and the stale 99 in the map reaches nothing. A percentage
    # can only ever decorate a render that is actually in flight.
    assert "clip-state" not in drawn["settled"]
    assert "99%" not in drawn["settled"]
    assert 'aria-label="a wolf in the dark"' in drawn["settled"]


def test_a_poll_tick_carries_the_percentage_without_touching_the_project_it_is_holding():
    """The client half of "no manifest write on a progress tick".

    `PUT /api/projects/{id}` sends the project object back whole, so a percentage folded into
    `project.jobs[].progress` would be saved into the manifest by the Director's next ordinary
    save -- the generic full-project PUT is this codebase's repeat offender for exactly that.
    The number is held beside the project, and the project is left byte-identical."""
    carried = run_workspace("""
      state.project = __PROJECT__;
      await flush();
      const before = JSON.stringify(state.project);
      await app.pollRenderStatus();
      await flush();
      console.log(JSON.stringify({
        held: state.renderProgress,
        unchanged: JSON.stringify(state.project) === before,
        jobProgress: state.project.jobs.map((job) => job.progress),
      }));
    """.replace("__PROJECT__", poll_project(jobs=[{
            "id": "j1", "kind": "flux", "status": "running", "prompt_id": "pr1",
            "target_id": "a1", "seed": 7, "output_files": [], "error": "", "progress": 0,
        }])),
        responses={"/api/projects/p1/render-status": {"body": POLL_IN_FLIGHT}},
    )

    assert carried["held"] == {"a1": 42}
    assert carried["unchanged"] is True
    assert carried["jobProgress"] == [0]


def test_a_poll_tick_repaints_the_timeline_so_a_rendering_clip_shows_its_percentage():
    """The clip's half of the delivery, driven end to end rather than by setting state by hand.

    A tick that learns only "the sampler is on step seven" changes no shot and no asset, so the
    repaint has to be triggered by the percentage having moved -- and without that trigger the
    clip keeps yesterday's markup while the number sits unread in state. Driven through
    `pollRenderStatus` for exactly that reason."""
    project = {
        "id": "p1", "name": "Poll", "song": None, "messages": [], "assets": [],
        "shots": [{"id": "shot_a", "start": 0, "duration": 4, "prompt": "a wolf at the window",
                   "status": "running"}],
        "jobs": [{"id": "h1", "kind": "h3", "status": "running", "prompt_id": "ph1",
                  "target_id": "shot_a", "seed": 3, "output_files": [], "error": ""}],
    }
    report = {
        "active": True, "comfy_online": True,
        "jobs": project["jobs"],
        "shots": [{"shot_id": "shot_a", "status": "running", "latest_output": ""}],
        "assets": [], "song": None,
        "progress": [{"job_id": "h1", "prompt_id": "ph1", "percent": 65}],
    }
    drawn = run_workspace(f"""
      state.project = {json.dumps(project)};
      await flush();
      at('#shots-track').innerHTML = '';
      await app.pollRenderStatus();
      await flush();
      console.log(JSON.stringify({{
        track: at('#shots-track').innerHTML,
        held: state.renderProgress,
      }}));
    """, responses={"/api/projects/p1/render-status": {"body": report}})

    assert drawn["held"] == {"shot_a": 65}
    assert '<span class="clip-state">RENDERING 65%</span>' in drawn["track"]
    assert "65% of this render is done." in drawn["track"]


def test_switching_projects_clears_the_percentages_of_the_project_being_left():
    """A percentage is keyed by target id and belongs to one project's live renders. Carried
    across a switch it would be drawn under another project's name, which is the same class of
    error as the readiness report and the snap report being cleared on the same path."""
    switched = run_workspace("""
      state.project = __PROJECT__;
      state.renderProgress = { a1: 42 };
      await flush();
      const before = { ...state.renderProgress };
      await fire('#project-select:change', { target: { value: 'p2' } });
      await flush();
      console.log(JSON.stringify({ before, after: state.renderProgress, loaded: state.project.id }));
    """.replace("__PROJECT__", poll_project()),
        responses={
            "/api/projects/p2": {"body": {**json.loads(poll_project()), "id": "p2", "jobs": []}},
            "/api/projects/p2/readiness": {"body": {"blocked": [], "warnings": []}},
        },
    )

    assert switched["before"] == {"a1": 42}
    assert switched["loaded"] == "p2"
    assert switched["after"] == {}


def test_the_percentage_is_dropped_when_the_render_settles_and_when_the_project_changes():
    """Two ways a stale number could outlive the render that earned it. The map is rebuilt whole
    from each answer -- never merged -- and cleared outright on a project load, so a percentage
    can never be drawn under another project's name."""
    dropped = run_workspace("""
      state.project = __PROJECT__;
      await flush();
      await app.pollRenderStatus();
      await flush();
      const rendering = { ...state.renderProgress };

      responses.set('/api/projects/p1/render-status', { body: __COMPLETION__ });
      state.project.jobs[0].status = 'running';
      await app.pollRenderStatus();
      await flush();
      console.log(JSON.stringify({ rendering, settled: state.renderProgress }));
    """.replace("__PROJECT__", poll_project())
       .replace("__COMPLETION__", json.dumps({**POLL_COMPLETION, "progress": []})),
        responses={"/api/projects/p1/render-status": {"body": POLL_IN_FLIGHT}},
    )

    assert dropped["rendering"] == {"a1": 42}
    assert dropped["settled"] == {}


# ---------------------------------------------------------------------------------------------
# The timeline's viewport: the zoom scale, its slider, and what the wheel means.
#
# The Director's report, 2026-08-20: "I cant scroll left or right and i see what i think is a zoom
# slider that isnt functional." The scroll half is a layout fact and is gated in a real browser by
# `tests/e2e_timeline_scroll.py` -- a stub DOM has no layout and structurally cannot see a
# scrollbar laid out below the bottom of the window. What *is* provable here is the arithmetic the
# controls run on, which is why it was written as pure functions in api.js rather than inline in a
# handler: a zoom that jumps to zero and a wheel that means the wrong thing are both decidable
# without a screen.


def test_the_edit_tools_and_the_zoom_live_in_the_bar_under_the_monitor():
    """The Director's report, 2026-08-21: "Timeline functions like +Shot, Split, Duplicate, Delete,
    and the zoom window are up by the Director Timeline header instead of down just below the [view]
    window in that bar." The panel heading is not where an editor reaches for them -- the bar under
    the picture is, because that is next to the thing being manipulated.

    Asserted by containment rather than by adjacency: every one of the six controls must be inside
    `.timeline-transport`, and none of them may be back in `.workspace-heading`."""
    markup = INDEX_HTML.read_text(encoding="utf-8")
    heading = re.search(
        r'<div class="workspace-heading compact">.*?<!-- Snap cuts', markup, re.DOTALL
    )
    assert heading, "the timeline panel's heading is gone"
    transport = re.search(
        r'<div class="timeline-transport">.*?<div class="timeline-scroll"', markup, re.DOTALL
    )
    assert transport, "the bar under the Monitor is gone"
    for control in ("add-shot", "split-shot", "duplicate-shot", "delete-shot",
                    "zoom-out", "zoom-slider", "zoom-in", "zoom-label"):
        assert f'id="{control}"' in transport.group(0), (
            f"#{control} is not in the bar under the Monitor"
        )
        assert f'id="{control}"' not in heading.group(0), (
            f"#{control} is back up in the panel heading, which is where the Director found it"
        )
    # The transport's own controls did not move out to make room.
    for control in ("timeline-start", "timeline-play", "timeline-time", "mute-song",
                    "mute-video", "master-volume", "timeline-duration"):
        assert f'id="{control}"' in transport.group(0), f"#{control} left the transport bar"


def test_the_timeline_has_a_zoom_slider_beside_its_zoom_buttons():
    """The control the Director went looking for. What was there was `#master-volume`, a working
    volume slider in the same bar, which is why the report says "what I think is a zoom slider" --
    so a real one is added rather than the volume one repurposed, and both are asserted here so a
    future edit cannot quietly resolve the confusion by deleting the wrong one."""
    markup = INDEX_HTML.read_text(encoding="utf-8")
    tools = re.search(r'<div class="timeline-tools">.*?</div>', markup, re.DOTALL)
    assert tools, "the timeline's tool row is gone"
    assert 'id="zoom-slider"' in tools.group(0), (
        "the zoom slider is not in the timeline's tool row beside the zoom buttons"
    )
    assert 'id="zoom-out"' in tools.group(0) and 'id="zoom-in"' in tools.group(0), (
        "the +/- zoom buttons were removed; the slider is an addition, not a replacement"
    )
    assert 'type="range"' in tools.group(0)
    assert 'aria-label="Timeline zoom"' in tools.group(0), (
        "the slider has no accessible name, and a bare range input announces nothing"
    )
    # The volume slider is still what it was, and is not the zoom. Its accessible name is pinned
    # exactly rather than by substring: an exact pin is what makes this able to catch the next
    # accidental change. The tooltip is deliberately the longer sentence -- reworded 2026-08-21 to
    # say what the control does *and* that it is session-only, because "Master song volume" alone
    # never explained why nothing about it survives a reload.
    assert 'id="master-volume"' in markup
    assert 'aria-label="Master song volume"' in markup, (
        "the volume slider has no accessible name; a `title` is not one, and the label it paints "
        "is the abbreviation VOL"
    )
    assert (
        'title="Master song volume \u2014 how loud the master song plays. Session-only; never '
        'saved to the project."'
    ) in markup, "the volume tooltip was reworded; update this pin deliberately or put it back"
    assert 'id="master-volume"' not in tools.group(0)


def test_both_sliders_in_the_bar_carry_a_visible_label():
    """Two unlabelled sliders in one row is exactly the confusion the report came from: the Director
    was looking at `#master-volume` and reading it as a zoom. A `title` is not enough -- it appears
    only on hover, and the control beside it announces itself in plain text on the button face.

    Each label is a real `<label for>`, so it is announced, and clicking the word focuses the slider
    rather than being decorative text that happens to sit nearby."""
    markup = INDEX_HTML.read_text(encoding="utf-8")
    labels = dict(re.findall(r'<label for="([^"]+)">([^<]+)</label>', markup))
    for slider, expected in (("zoom-slider", "ZOOM"), ("master-volume", "VOL")):
        assert slider in labels, f"#{slider} has no visible label, only a tooltip"
        assert labels[slider].strip() == expected, (slider, labels[slider])
    # Both sit inside a `.slider-field`, which is what pairs the word with the control visually.
    for slider in ("zoom-slider", "master-volume"):
        field = re.search(
            rf'<span class="slider-field"><label for="{slider}">.*?</span>', markup, re.DOTALL
        )
        assert field and f'id="{slider}"' in field.group(0), (
            f"#{slider}'s label is not grouped with the slider it names"
        )
    # Each slider also carries an accessible name that *contains* the abbreviation it paints, so
    # the word a Director reads and the word a screen reader announces are one name.
    names = dict(re.findall(r'id="(zoom-slider|master-volume)"[^>]*aria-label="([^"]+)"', markup))
    assert names == {"zoom-slider": "Timeline zoom", "master-volume": "Master song volume"}, names
    for slider, painted in (("zoom-slider", "ZOOM"), ("master-volume", "VOL")):
        assert painted.lower() in names[slider].lower().replace(" ", ""), (
            f"#{slider} paints {painted!r} and announces {names[slider]!r}, which are two names "
            "for one control"
        )
    styles = STYLES_CSS.read_text(encoding="utf-8")
    assert ".slider-field > label" in styles, "the slider labels have no style of their own"


def test_the_icon_only_edit_tools_carry_their_meaning_in_words_as_well_as_a_glyph():
    """The Director's ruling on the cramped bar (2026-08-21): "use button icons with tooltips when
    needed". A glyph is not a label -- this stylesheet's own rule is that state is never carried by
    colour alone, and the same holds for a picture. So every icon button announces the same sentence
    its tooltip shows, and Delete keeps its destructive styling, because an icon-only destructive
    control that looks like the two beside it is a foot-gun on a bar used constantly.

    The add button deliberately keeps its word: a bare + is what the zoom-in button in this same bar
    already says, and "the meaning is genuinely unambiguous" is the test for dropping a word."""
    markup = INDEX_HTML.read_text(encoding="utf-8")
    tools = re.search(r'<div class="timeline-tools">.*?</div>', markup, re.DOTALL).group(0)
    for control in ("split-shot", "duplicate-shot", "delete-shot"):
        button = re.search(rf'<button[^>]*id="{control}"[^>]*>([^<]*)</button>', tools)
        assert button, f"#{control} is not a button in the tool row any more"
        attributes = dict(re.findall(r'(\w[\w-]*)="([^"]*)"', button.group(0)))
        assert attributes.get("aria-label"), (
            f"#{control} shows the glyph {button.group(1)!r} and announces nothing"
        )
        assert attributes["aria-label"] == attributes.get("title"), (
            f"#{control}'s tooltip and its accessible name are different sentences: {attributes}"
        )
        assert "icon-tool" in attributes.get("class", ""), attributes
    # The destructive one is still styled as destructive, not as one more glyph in a row.
    delete = re.search(r'<button[^>]*id="delete-shot"[^>]*>', tools).group(0)
    assert "danger-button" in delete, delete
    for safe in ("split-shot", "duplicate-shot"):
        assert "danger-button" not in re.search(
            rf'<button[^>]*id="{safe}"[^>]*>', tools
        ).group(0)
    # And the add button keeps its word, because the glyph it would use is taken.
    add = re.search(r'<button[^>]*id="add-shot"[^>]*>([^<]*)</button>', tools)
    assert "Shot" in add.group(1), add.group(1)
    assert "icon-tool" not in add.group(0), "the add button went icon-only beside a zoom-in +"


def test_the_zoom_sliders_markup_bounds_are_the_helper_the_handler_reads_it_with():
    """A slider whose markup range disagreed with `zoomFromSlider` would map its own travel onto
    part of the scale, and the mismatch would show only at one end of the drag. The default value
    the markup ships is asserted against `zoomSliderValue(TIMELINE_ZOOM_BASE)` for the same reason:
    it is the thumb position before the first render writes one, and 100% is where the timeline
    opens."""
    bounds = run_module("""
      import { TIMELINE_ZOOM_BASE, TIMELINE_ZOOM_SLIDER_MAX, TIMELINE_LABEL_WIDTH, zoomSliderValue }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        sliderMax: TIMELINE_ZOOM_SLIDER_MAX, label: TIMELINE_LABEL_WIDTH,
        atBase: zoomSliderValue(TIMELINE_ZOOM_BASE),
      }));
    """)
    markup = INDEX_HTML.read_text(encoding="utf-8")
    slider = re.search(r'<input type="range" id="zoom-slider"[^>]*>', markup)
    assert slider, "the zoom slider is not a range input any more"
    attributes = dict(re.findall(r'(\w[\w-]*)="([^"]*)"', slider.group(0)))
    assert int(attributes["min"]) == 0
    assert int(attributes["max"]) == bounds["sliderMax"], (attributes, bounds)
    assert int(attributes["value"]) == bounds["atBase"], (
        "the slider's shipped thumb position is not where 100% zoom sits on its own scale",
        attributes, bounds,
    )
    # The label gutter every pixel<->second conversion offsets by, held against the stylesheet
    # that makes it real. A drift here silently moves every clip by 90px worth of seconds.
    styles = STYLES_CSS.read_text(encoding="utf-8")
    assert f"grid-template-columns: {bounds['label']}px 1fr" in styles, (
        "TIMELINE_LABEL_WIDTH no longer matches `.track`'s label column"
    )


def test_the_zoom_slider_covers_the_whole_scale_and_round_trips():
    """Executed, not read. The mapping is logarithmic so a notch is the same proportional change
    at either end -- a linear one spends four fifths of its travel above 100% and leaves the
    readable half unpickable. Both ends must land exactly on the clamps, or the slider cannot
    reach a zoom the buttons can."""
    measured = run_module("""
      import { TIMELINE_ZOOM_BASE, TIMELINE_ZOOM_MAX, TIMELINE_ZOOM_MIN,
        TIMELINE_ZOOM_SLIDER_MAX, zoomFromSlider, zoomLabelText, zoomSliderValue }
        from './src/music_video_producer/web/assets/api.js';
      const roundTrip = [6, 8, 11, 16, 24, 32, 48, 64].map(
        (scale) => zoomFromSlider(zoomSliderValue(scale))
      );
      console.log(JSON.stringify({
        floor: zoomFromSlider(0),
        ceiling: zoomFromSlider(TIMELINE_ZOOM_SLIDER_MAX),
        below: zoomFromSlider(-500),
        above: zoomFromSlider(TIMELINE_ZOOM_SLIDER_MAX * 4),
        rubbish: zoomFromSlider('not a number'),
        roundTrip,
        // Equal proportional steps across the travel: the ratio between neighbouring quarters is
        // the same number everywhere on a logarithmic scale, and is not on a linear one.
        quarters: [0, 0.25, 0.5, 0.75, 1].map(
          (part) => zoomFromSlider(part * TIMELINE_ZOOM_SLIDER_MAX)
        ),
        labels: [zoomLabelText(TIMELINE_ZOOM_BASE), zoomLabelText(TIMELINE_ZOOM_MIN),
                 zoomLabelText(TIMELINE_ZOOM_MAX)],
        clamps: [TIMELINE_ZOOM_MIN, TIMELINE_ZOOM_MAX],
      }));
    """)

    floor, ceiling = measured["clamps"]
    assert measured["floor"] == pytest.approx(floor)
    assert measured["ceiling"] == pytest.approx(ceiling)
    # Out-of-range input is clamped rather than trusted: a range input can be driven by keyboard,
    # and nothing here may put the timeline at 0 px/s.
    assert measured["below"] == pytest.approx(floor)
    assert measured["above"] == pytest.approx(ceiling)
    assert measured["rubbish"] == pytest.approx(floor)

    for asked, got in zip([6, 8, 11, 16, 24, 32, 48, 64], measured["roundTrip"]):
        assert got == pytest.approx(asked, rel=0.02), (asked, got)

    ratios = [
        measured["quarters"][index + 1] / measured["quarters"][index]
        for index in range(len(measured["quarters"]) - 1)
    ]
    for ratio in ratios[1:]:
        assert ratio == pytest.approx(ratios[0], rel=0.01), (
            "the slider's travel is not proportional, so a notch means something different at "
            f"each end of it: {measured['quarters']}"
        )

    assert measured["labels"] == ["100%", "38%", "400%"]


def test_zooming_holds_the_playhead_when_it_is_on_screen_and_the_centre_when_it_is_not():
    """The anchor rule, executed. Zooming a 30-shot timeline back to the head of the song every
    time is its own usability defect, and it is the one the +/- buttons had -- they wrote the new
    scale and left the scroll offset where it was, which at 195% puts the viewport somewhere
    nobody asked for.

    The playhead wins while it is visible because it is the timeline's subject: the Monitor plays
    the shot under it, so it is the frame being judged and it must not move. Scrolled away it is
    not on screen at all, and holding an off-screen second still would move everything the
    Director *is* reading -- so there the middle of the visible band is the honest invariant."""
    zoomed = run_module("""
      import { TIMELINE_LABEL_WIDTH, TIMELINE_ZOOM_ANCHORS, zoomViewport }
        from './src/music_video_producer/web/assets/api.js';
      const gutter = TIMELINE_LABEL_WIDTH;
      // A viewport 1000px wide, 900px along a timeline drawn at 16 px/s. The playhead is parked
      // at 4s -- pixel 154 -- which is behind the left edge, so the centre anchors.
      const away = zoomViewport({
        scrollLeft: 900, viewportWidth: 1000, pixelsPerSecond: 16, toPixelsPerSecond: 32,
        playheadSeconds: 4,
      });
      // The same viewport with the playhead at 70s -- pixel 1210, comfortably inside it.
      const onScreen = zoomViewport({
        scrollLeft: 900, viewportWidth: 1000, pixelsPerSecond: 16, toPixelsPerSecond: 32,
        playheadSeconds: 70,
      });
      // Zooming out from the same place, and from the head of the song where there is nowhere
      // left to go.
      const out = zoomViewport({
        scrollLeft: 900, viewportWidth: 1000, pixelsPerSecond: 16, toPixelsPerSecond: 8,
        playheadSeconds: 4,
      });
      const atHead = zoomViewport({
        scrollLeft: 0, viewportWidth: 1000, pixelsPerSecond: 16, toPixelsPerSecond: 32,
        playheadSeconds: 0,
      });
      const secondsAtCentre = (plan, scale) => (plan.scrollLeft + 1000 / 2 - gutter) / scale;
      const screenXOfPlayhead = (plan, scale, seconds) =>
        gutter + seconds * scale - plan.scrollLeft;
      console.log(JSON.stringify({
        anchors: TIMELINE_ZOOM_ANCHORS,
        away: {
          ...away,
          centreBefore: (900 + 500 - gutter) / 16,
          centreAfter: secondsAtCentre(away, 32),
        },
        onScreen: {
          ...onScreen,
          screenBefore: gutter + 70 * 16 - 900,
          screenAfter: screenXOfPlayhead(onScreen, 32, 70),
        },
        out: {
          ...out,
          centreBefore: (900 + 500 - gutter) / 16,
          centreAfter: secondsAtCentre(out, 8),
        },
        atHead,
      }));
    """)

    assert zoomed["away"]["anchor"] == zoomed["anchors"]["centre"]
    assert zoomed["away"]["centreAfter"] == pytest.approx(zoomed["away"]["centreBefore"])
    assert zoomed["away"]["scrollLeft"] > 0, (
        "zooming in threw a scrolled timeline back to the head of the song"
    )

    assert zoomed["onScreen"]["anchor"] == zoomed["anchors"]["playhead"]
    assert zoomed["onScreen"]["screenAfter"] == pytest.approx(zoomed["onScreen"]["screenBefore"])
    assert zoomed["onScreen"]["anchorSeconds"] == pytest.approx(70)

    # Zooming out holds the same second, and the offset shrinks with the content rather than
    # running off the front of it.
    assert zoomed["out"]["centreAfter"] == pytest.approx(zoomed["out"]["centreBefore"])
    assert zoomed["out"]["scrollLeft"] >= 0

    # Already at the head with the playhead at zero: nothing moves, and nothing goes negative.
    assert zoomed["atHead"]["scrollLeft"] == 0
    assert zoomed["atHead"]["anchor"] == zoomed["anchors"]["playhead"]


def test_one_wheel_notch_over_the_tracks_scrolls_along_the_song():
    """What the plain wheel means, executed. It scrolls along the song -- the convention in every
    editing application the Director already uses, and the direct answer to "I cant scroll left or
    right", because the axis this panel is about is time.

    The rule that was written first was "hijack deltaY only when there is nothing to scroll
    vertically", and the browser measured it wrong: at 1600x1100 the four tracks overflowed their
    box by *four pixels*, which was enough to hand the wheel straight back and leave the gesture as
    dead as it was before. Shift is the way back to vertical, and a box with nothing to scroll
    horizontally keeps the browser's own behaviour."""
    meant = run_module("""
      import { TIMELINE_WHEEL_ACTIONS, timelineWheelPlan }
        from './src/music_video_producer/web/assets/api.js';
      const wide = { canScrollX: true, canScrollY: true };
      console.log(JSON.stringify({
        actions: TIMELINE_WHEEL_ACTIONS,
        // The four-pixel case the browser found: vertical overflow exists and the plain wheel
        // must still scroll along the song.
        plain: timelineWheelPlan({ deltaY: 120, ...wide }),
        fitsVertically: timelineWheelPlan({ deltaY: 120, canScrollX: true }),
        ctrl: timelineWheelPlan({ deltaY: -120, ctrlKey: true, ...wide }),
        meta: timelineWheelPlan({ deltaY: -120, metaKey: true, ...wide }),
        // Ctrl zooms even where there is nothing to scroll, so the gesture never depends on how
        // long the plan happens to be.
        ctrlOnAShortPlan: timelineWheelPlan({ deltaY: -120, ctrlKey: true }),
        trackpadSwipe: timelineWheelPlan({ deltaX: -80, deltaY: 0, ...wide }),
        shift: timelineWheelPlan({ deltaY: 120, shiftKey: true, ...wide }),
        shiftWithNothingBelow: timelineWheelPlan({ deltaY: 120, shiftKey: true, canScrollX: true }),
        nothingToScroll: timelineWheelPlan({ deltaY: 120, canScrollY: true }),
        nothingAtAll: timelineWheelPlan({}),
      }));
    """)
    actions = meant["actions"]

    assert meant["plain"]["action"] == actions["scroll"]
    assert meant["plain"]["scrollX"] == 120 and meant["plain"]["scrollY"] == 0
    assert meant["fitsVertically"] == meant["plain"]

    for key in ("ctrl", "meta", "ctrlOnAShortPlan"):
        assert meant[key]["action"] == actions["zoom"], key
        assert meant[key]["delta"] == -120, key
        assert meant[key]["scrollX"] == 0, key

    assert meant["trackpadSwipe"]["action"] == actions["scroll"]
    assert meant["trackpadSwipe"]["scrollX"] == -80

    # Shift is the escape hatch back to vertical, because the plain wheel is taken.
    assert meant["shift"]["action"] == actions["scroll"]
    assert meant["shift"]["scrollY"] == 120 and meant["shift"]["scrollX"] == 0
    # And with nothing below, shift does nothing rather than scrolling sideways by surprise.
    assert meant["shiftWithNothingBelow"]["action"] == actions["native"]

    # A plan that fits its box behaves exactly as any other page does.
    assert meant["nothingToScroll"]["action"] == actions["native"]
    assert meant["nothingAtAll"]["action"] == actions["native"]


def test_the_zoom_slider_is_bound_to_something_that_redraws_the_timeline():
    """The slider executed through the workspace, because "wired to nothing that reads" is exactly
    what the Director suspected. Dragging it must change what `renderTimeline` draws -- the clip
    geometry, not merely a number in `state` -- and the thumb must follow the +/- buttons so the
    three controls can never be left disagreeing."""
    shots = [
        {"id": "shot_a", "start": 0, "duration": 4, "prompt": "a wolf at the window"},
        {"id": "shot_b", "start": 4, "duration": 4, "prompt": "a wolf in the snow"},
    ]
    driven = run_workspace(f"""
      import {{ TIMELINE_ZOOM_SLIDER_MAX, zoomFromSlider }}
        from './src/music_video_producer/web/assets/api.js';
      state.project = {{ id: 'p1', shots: {json.dumps(shots)}, jobs: [], assets: [], sections: [] }};
      const widthOf = (id) => {{
        const html = at('#shots-track').innerHTML;
        const clip = html.split('data-shot-id="' + id + '"')[1] || '';
        return (/width:([0-9.]+)px/.exec(clip) || [null, ''])[1];
      }};
      fire('#zoom-in:click');
      const before = {{ scale: state.pixelsPerSecond, thumb: at('#zoom-slider').value,
                        width: widthOf('shot_b'), label: at('#zoom-label').textContent }};
      // Dragged to three quarters of its travel, the way a Director drags it.
      const wanted = Math.round(0.75 * TIMELINE_ZOOM_SLIDER_MAX);
      at('#zoom-slider').value = String(wanted);
      fire('#zoom-slider:input', {{ target: at('#zoom-slider') }});
      const after = {{ scale: state.pixelsPerSecond, thumb: at('#zoom-slider').value,
                       width: widthOf('shot_b'), label: at('#zoom-label').textContent }};
      fire('#zoom-out:click');
      const zoomedOut = {{ scale: state.pixelsPerSecond, thumb: at('#zoom-slider').value,
                           width: widthOf('shot_b') }};
      console.log(JSON.stringify({{
        before, after, zoomedOut, asked: wanted, expected: zoomFromSlider(wanted),
      }}));
    """)

    assert driven["after"]["scale"] == pytest.approx(driven["expected"])
    assert driven["after"]["scale"] != driven["before"]["scale"]
    # The clip is redrawn at the new scale: the slider reaches the render, not just `state`.
    assert float(driven["after"]["width"]) == pytest.approx(4 * driven["expected"], rel=0.01)
    assert driven["after"]["label"] != driven["before"]["label"]
    # And the thumb is written back from the scale by `renderTimeline`, so a button moves it too.
    assert int(driven["after"]["thumb"]) == pytest.approx(driven["asked"], abs=2)
    assert driven["zoomedOut"]["scale"] < driven["after"]["scale"]
    assert int(driven["zoomedOut"]["thumb"]) < int(driven["after"]["thumb"])
    assert float(driven["zoomedOut"]["width"]) < float(driven["after"]["width"])


# ------------------------------------------------------------------------------------------
# Replace With / Cancel: the browser half of the way through the delete refusal. The pure
# decisions are executed under node, and the affordance is *run* against the stub DOM with its
# markup read afterwards — never grepped for in `app.js`, which is the recorded incident.
# ------------------------------------------------------------------------------------------


def _replace_asset(asset_id: str, name: str, kind: str, source: str) -> dict:
    return {
        "id": asset_id, "name": name, "kind": kind, "source": source,
        "path": f"media/assets/{asset_id}.png", "prompt": "", "prompt_id": "",
        "created_at": "2026-08-20T10:00:00Z", "consistency_prompt": "", "vision": None,
    }


REPLACE_PROJECT = {
    "id": "p1",
    "jobs": [],
    "song": None,
    "assets": [
        _replace_asset("a_lucy", "Lucy", "character", "upload"),
        _replace_asset("a_sheet", "Lucy multiview", "character", "krea-multiview"),
        _replace_asset("a_room", "Dusk Warehouse", "setting", "upload"),
    ],
    "shots": [
        {"id": "s0", "start": 0, "duration": 5, "prompt": "One",
         "citations": [{"asset_id": "a_lucy", "role": "reference", "order": 0}]},
        {"id": "s1", "start": 5, "duration": 5, "prompt": "Two",
         "citations": [{"asset_id": "a_lucy", "role": "reference", "order": 0},
                       {"asset_id": "a_sheet", "role": "reference", "order": 1}]},
        {"id": "s2", "start": 10, "duration": 5, "prompt": "Three",
         "citations": [{"asset_id": "a_room", "role": "reference", "order": 0}]},
    ],
}

DELETE_REFUSAL = (
    "Lucy is cited by SHOT 01 (s0), SHOT 02 (s1), and deleting it would leave those "
    "citations dangling — the render would refuse them one at a time. Remove it from those "
    "shots first."
)

REPLACE_REPORT = {
    "applied": False,
    "replaced": "Lucy",
    "replacement": "Lucy multiview",
    "swapped": 1,
    "merged": 1,
    "skipped": 1,
    "still_cited": 1,
    "rendered": 1,
    "approved": 0,
    "notes": ["1 shot(s) already hold a take that was rendered against Lucy: SHOT 01 (s0)."],
    "swaps": [{"shot_id": "s0", "label": "SHOT 01 (s0)", "roles": ["reference"],
               "carried_label": "Lucy", "provenance": "rendered"}],
    "merges": [{"shot_id": "s1", "label": "SHOT 02 (s1)", "roles": ["reference"],
                "carried_label": "", "provenance": ""}],
    "skips": [{"shot_id": "s3", "label": "SHOT 04 (s3)",
               "reason": "Left unchanged because they are locked: SHOT 04 (s3)."}],
    "warning": "",
    "message": "Lucy multiview would replace Lucy in 1 shot(s); 1 shot(s) already cite it.",
    # Carried on both stages of the canned answer so one entry serves the report and the apply;
    # the workspace only reads it when it asked to apply.
    "project": {**REPLACE_PROJECT, "assets": REPLACE_PROJECT["assets"]},
}


def test_the_replace_with_menu_never_offers_the_asset_being_removed():
    """A menu entry for the asset itself is a control whose only outcome is the route's 422.

    `assetIsCited` is asserted beside it because it is what decides the affordance appears at
    all — read from the manifest the browser already holds, never matched against the refusal's
    prose, so a reworded refusal cannot make the answer vanish.
    """
    decisions = run_module("""
      import { assetReplacementOptions, assetIsCited }
        from './src/music_video_producer/web/assets/api.js';
      const project = __PROJECT__;
      console.log(JSON.stringify({
        offered: assetReplacementOptions(project, 'a_lucy').map((asset) => asset.id),
        forSheet: assetReplacementOptions(project, 'a_sheet').map((asset) => asset.id),
        empty: assetReplacementOptions(null, 'a_lucy'),
        cited: assetIsCited(project, 'a_lucy'),
        uncited: assetIsCited(project, 'a_missing'),
        noProject: assetIsCited(null, 'a_lucy'),
      }));
    """.replace("__PROJECT__", json.dumps(REPLACE_PROJECT)))

    assert decisions["offered"] == ["a_sheet", "a_room"]
    assert decisions["forSheet"] == ["a_lucy", "a_room"]
    assert decisions["empty"] == []
    assert decisions["cited"] is True
    assert decisions["uncited"] is False
    assert decisions["noProject"] is False


def test_the_replace_control_decides_its_two_stages():
    """Executed, not read. The same button reports until a report exists and applies after.

    The nothing-to-do case is the one that matters: a report whose shots are all skips must not
    offer an apply, because there is nothing for it to write.
    """
    decisions = run_module("""
      import { assetReplacementControl, REPLACE_WITH_LABEL, REPLACE_WITH_UNCHOSEN,
               REPLACE_WITH_NOTHING_TO_DO }
        from './src/music_video_producer/web/assets/api.js';
      const report = __REPORT__;
      console.log(JSON.stringify({
        unchosen: assetReplacementControl('', null),
        unchosenWithReport: assetReplacementControl('', report),
        chosen: assetReplacementControl('a_sheet', null),
        reported: assetReplacementControl('a_sheet', report),
        allSkipped: assetReplacementControl('a_sheet',
          { ...report, swapped: 0, merged: 0, skipped: 3 }),
        wording: { REPLACE_WITH_LABEL, REPLACE_WITH_UNCHOSEN, REPLACE_WITH_NOTHING_TO_DO },
      }));
    """.replace("__REPORT__", json.dumps(REPLACE_REPORT)))

    wording = decisions["wording"]
    assert decisions["unchosen"] == {
        "disabled": True, "apply": False,
        "label": wording["REPLACE_WITH_LABEL"], "reason": wording["REPLACE_WITH_UNCHOSEN"],
    }
    # A report in hand does not make an unchosen menu runnable.
    assert decisions["unchosenWithReport"]["disabled"] is True
    assert decisions["chosen"] == {
        "disabled": False, "apply": False, "label": wording["REPLACE_WITH_LABEL"], "reason": "",
    }
    assert decisions["reported"]["apply"] is True
    assert decisions["reported"]["label"] == "Replace in 2 shot(s)"
    assert decisions["reported"]["reason"] == REPLACE_REPORT["message"]
    assert decisions["allSkipped"]["apply"] is False
    assert decisions["allSkipped"]["disabled"] is True
    assert decisions["allSkipped"]["reason"] == wording["REPLACE_WITH_NOTHING_TO_DO"]


def test_the_replacement_report_lines_carry_every_bucket_and_every_skip_reason_verbatim():
    """All three lists in full. A skip's line is the server's own sentence, unedited.

    The refusals are decided once, in Python, and a client that paraphrased one would be a
    second opinion that can drift from the rule that actually stops the write.
    """
    lines = run_module("""
      import { assetReplacementReportLines }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        lines: assetReplacementReportLines(__REPORT__),
        empty: assetReplacementReportLines(null),
      }));
    """.replace("__REPORT__", json.dumps(REPLACE_REPORT)))

    assert lines["empty"] == []
    assert [line["kind"] for line in lines["lines"]] == ["note", "swap", "merge", "skip"]
    # The take-provenance sentence is the server's, drawn above the lists rather than among the
    # skips: those shots are being changed, so a line among the skips would say the opposite.
    assert lines["lines"][0]["text"] == REPLACE_REPORT["notes"][0]
    assert lines["lines"][1]["text"] == (
        'SHOT 01 (s0): reference · label "Lucy" carried · has a take rendered against Lucy'
    )
    # The "already have" line says what happens in the Director's own terms: the standing
    # citation stays and the old one is removed. No take behind it, so no marker.
    assert lines["lines"][2]["text"] == (
        "SHOT 02 (s1): already cites Lucy multiview, so the reference citation of Lucy is removed"
    )
    assert lines["lines"][3]["text"] == REPLACE_REPORT["skips"][0]["reason"]


def test_a_refused_delete_offers_replace_with_and_only_the_second_click_applies():
    """The whole affordance, driven against the stub DOM and read as markup.

    The delete is refused; the refusal itself is unchanged and stays on screen; the Replace With
    menu appears beneath it without the asset being removed; the first click on the button
    fetches a report with `confirm_apply` false; the report is drawn in full; and only then does
    the same button become the apply.
    """
    run = run_workspace(
        """
      state.project = __PROJECT__;
      state.selectedAssetId = 'a_lucy';
      app.renderAssetInspector();
      const beforeDelete = at('#asset-inspector').innerHTML;
      answer(true);
      await fire('#delete-asset:click');
      await flush();
      const refused = at('#asset-inspector').innerHTML;
      const deleteRequest = requests[0];
      fire('#replace-with:change', { currentTarget: { value: 'a_sheet' } });
      const chosen = at('#asset-inspector').innerHTML;
      requests.length = 0;
      await fire('#replace-run:click');
      await flush();
      const reported = at('#asset-inspector').innerHTML;
      const reportRequest = requests[0];
      requests.length = 0;
      await fire('#replace-run:click');
      await flush();
      const applyRequest = requests[0];
      const afterApply = at('#asset-inspector').innerHTML;
      console.log(JSON.stringify({
        beforeDelete, refused, chosen, reported, afterApply,
        deleteRequest, reportRequest, applyRequest,
      }));
    """.replace("__PROJECT__", json.dumps(REPLACE_PROJECT)),
        responses={
            "/api/projects/p1/assets/a_lucy": {"status": 422, "body": {"detail": DELETE_REFUSAL}},
            "/api/projects/p1/assets/a_lucy/replace-citations": {"body": REPLACE_REPORT},
        },
    )

    # Nothing about replacing is on screen until a delete has actually been refused.
    assert 'id="delete-asset"' in run["beforeDelete"]
    assert "replace-panel" not in run["beforeDelete"]
    assert run["deleteRequest"]["method"] == "DELETE"

    # The refusal is unchanged and stays readable beside the way through it.
    assert DELETE_REFUSAL in run["refused"]
    assert "replace-panel" in run["refused"]
    assert 'id="replace-with"' in run["refused"]
    assert 'id="replace-cancel"' in run["refused"]
    # The menu offers the other two assets and never the one being removed.
    assert '<option value="a_sheet"' in run["refused"]
    assert '<option value="a_room"' in run["refused"]
    assert '<option value="a_lucy"' not in run["refused"]
    # Unchosen, the button is shut and says why; no report has been fetched.
    assert "Pick the asset that takes over." in run["refused"]
    assert "snap-report" not in run["refused"]
    assert 'id="replace-run" disabled' in run["refused"]

    # Chosen but unreported: runnable, still not an apply.
    assert 'id="replace-run"' in run["chosen"]
    assert 'id="replace-run" disabled' not in run["chosen"]
    assert "Report the replacement" in run["chosen"]
    assert "snap-report" not in run["chosen"]

    # Stage one asked for a report and wrote nothing.
    assert run["reportRequest"]["path"] == "/api/projects/p1/assets/a_lucy/replace-citations"
    assert json.loads(run["reportRequest"]["body"]) == {
        "replacement_id": "a_sheet", "confirm_apply": False
    }
    # Stage two: the whole report on screen — all three headings, every line, every skip reason.
    assert "snap-report" in run["reported"]
    assert "Would be replaced (1)" in run["reported"]
    assert "Already cite the replacement (1)" in run["reported"]
    assert "Would be left alone (1)" in run["reported"]
    assert "SHOT 01 (s0): reference" in run["reported"]
    assert "already cites Lucy multiview" in run["reported"]
    assert REPLACE_REPORT["skips"][0]["reason"] in run["reported"]
    # The take-provenance note is on screen, and it is set apart from the skip list rather than
    # drawn as one — those shots are being changed.
    assert "snap-note" in run["reported"]
    assert REPLACE_REPORT["notes"][0] in run["reported"]
    assert "Replace in 2 shot(s)" in run["reported"]
    # Only the second click carries the confirmation.
    assert json.loads(run["applyRequest"]["body"]) == {
        "replacement_id": "a_sheet", "confirm_apply": True
    }
    # And the affordance closes once it has been applied.
    assert "replace-panel" not in run["afterApply"]


def test_the_assets_panel_offers_replace_in_shots_only_for_a_cited_asset():
    """`replaceInShotsControl`, executed. The Director's second entry point, decided by the count.

    The browser already knows how many shots cite an asset, so an uncited asset gets no button at
    all rather than one whose only outcome is the route's 422.
    """
    decisions = run_module("""
      import { replaceInShotsControl, citingShotCount }
        from './src/music_video_producer/web/assets/api.js';
      const project = __PROJECT__;
      console.log(JSON.stringify({
        cited: replaceInShotsControl(project, 'a_lucy'),
        once: replaceInShotsControl(project, 'a_room'),
        uncited: replaceInShotsControl(project, 'a_sheet_alone'),
        noProject: replaceInShotsControl(null, 'a_lucy'),
        counted: citingShotCount(project, 'a_sheet'),
      }));
    """.replace("__PROJECT__", json.dumps(REPLACE_PROJECT)))

    assert decisions["cited"] == {"shown": True, "count": 2, "label": "Replace in 2 shot(s) with…"}
    assert decisions["once"] == {"shown": True, "count": 1, "label": "Replace in 1 shot(s) with…"}
    assert decisions["uncited"]["shown"] is False
    assert decisions["noProject"] == {
        "shown": False, "count": 0, "label": "Replace in 0 shot(s) with…"
    }
    assert decisions["counted"] == 1


def test_replace_in_shots_opens_the_same_affordance_with_no_delete_in_the_path():
    """The Assets-panel way in, driven against the stub DOM.

    Same panel, same two-stage button, and **no DELETE is ever sent** — the Director asked for
    "the same replacement function but without resulting in asset deletion". Because there is no
    refusal to read, the panel carries its own explanation instead.
    """
    run = run_workspace(
        """
      state.project = __PROJECT__;
      state.selectedAssetId = 'a_room';
      app.renderAssetInspector();
      const oneShot = at('#asset-inspector').innerHTML;
      state.selectedAssetId = 'a_lucy';
      app.renderAssetInspector();
      const closed = at('#asset-inspector').innerHTML;
      requests.length = 0;
      fire('#replace-in-shots:click');
      const opened = at('#asset-inspector').innerHTML;
      fire('#replace-with:change', { currentTarget: { value: 'a_sheet' } });
      await fire('#replace-run:click');
      await flush();
      const reported = at('#asset-inspector').innerHTML;
      console.log(JSON.stringify({
        oneShot, closed, opened, reported,
        methods: requests.map((entry) => entry.method),
        firstRequest: requests[0],
      }));
    """.replace("__PROJECT__", json.dumps(REPLACE_PROJECT)),
        responses={
            "/api/projects/p1/assets/a_lucy/replace-citations": {"body": REPLACE_REPORT},
        },
    )

    # The button is drawn beside "Attach to selected shot" and carries the count, because the
    # timeline is not visible from this panel.
    assert 'id="attach-asset"' in run["closed"]
    assert 'id="replace-in-shots"' in run["closed"]
    assert "Replace in 2 shot(s) with" in run["closed"]
    assert "Replace in 1 shot(s) with" in run["oneShot"]
    # Closed until it is clicked — no delete was attempted and none is needed.
    assert "replace-panel" not in run["closed"]
    assert "replace-panel" in run["opened"]
    # No refusal to show, so the panel explains itself.
    assert "Nothing is deleted and nothing is rendered" in run["opened"]
    assert "citations dangling" not in run["opened"]
    # Nothing but the report request was ever sent, and it is a POST.
    assert run["methods"] == ["POST"]
    assert "DELETE" not in run["methods"]
    assert json.loads(run["firstRequest"]["body"]) == {
        "replacement_id": "a_sheet", "confirm_apply": False
    }
    # And it is the same report, named shot by shot — which is what makes this control usable
    # from a panel where the timeline cannot be seen.
    assert "SHOT 01 (s0)" in run["reported"]
    assert "Replace in 2 shot(s)" in run["reported"]


def test_cancel_closes_the_affordance_without_sending_anything():
    """The other half of the option set the Director asked for, by name.

    Cancel is the whole point of "Replace With/Cancel": a Director who opened the menu and
    thought better of it must be able to leave without a request having been made.
    """
    run = run_workspace(
        """
      state.project = __PROJECT__;
      state.selectedAssetId = 'a_lucy';
      app.renderAssetInspector();
      answer(true);
      await fire('#delete-asset:click');
      await flush();
      fire('#replace-with:change', { currentTarget: { value: 'a_sheet' } });
      requests.length = 0;
      fire('#replace-cancel:click');
      const closed = at('#asset-inspector').innerHTML;
      console.log(JSON.stringify({ closed, sent: requests.length }));
    """.replace("__PROJECT__", json.dumps(REPLACE_PROJECT)),
        responses={
            "/api/projects/p1/assets/a_lucy": {"status": 422, "body": {"detail": DELETE_REFUSAL}},
        },
    )

    assert run["sent"] == 0
    assert "replace-panel" not in run["closed"]
    # The delete button is still there: cancelling a replacement is not cancelling the intent.
    assert 'id="delete-asset"' in run["closed"]
def test_a_take_row_is_a_button_over_its_whole_width_not_a_chip_at_the_end_of_a_line():
    """The Director's report, 2026-08-21: "i can see the takes in the shot info window but clicking
    on either does nothing instead of hot swapping between available shots."

    Driven in a browser before anything changed (`tests/e2e_take_swap.py`), and both halves of that
    sentence were measurable: clicking the **row** left `latest_output` untouched, clicking the
    **chip** switched it correctly. So the row model was never wrong and the handler was never
    stale -- the affordance was, and a 286px line of text with a 40px live button at its right end
    reads as broken while working.

    Executed through `renderShotInspector` rather than read, because "the row is the control" is a
    property of the markup that function produces. A real `<button>` is the whole point: focus,
    Enter and Space come from the platform instead of from a role/tabindex/keydown trio this file
    would have to keep correct, and `disabled` is what makes the current row read as deliberately
    closed rather than as one more piece of inert text."""
    jobs = [
        {"id": "job_1", "kind": "h3", "target_id": "shot_a", "status": "complete", "seed": 3,
         "output_files": ["shots/shot_a/take_1.mp4"],
         "updated_at": "2026-08-20T09:21:00Z"},
        {"id": "job_2", "kind": "h3", "target_id": "shot_a", "status": "complete", "seed": 104,
         "output_files": ["shots/shot_a/take_2.mp4"],
         "updated_at": "2026-08-20T11:08:00Z"},
    ]
    drawn = run_workspace(f"""
      state.project = {{ id: 'p1', jobs: {json.dumps(jobs)}, assets: [], shots: [
        {{ id: 'shot_a', start: 0, duration: 4, prompt: 'a wolf', status: 'complete',
           latest_output: 'shots/shot_a/take_2.mp4' }},
      ] }};
      state.selectedShotId = 'shot_a';
      app.renderShotInspector();
      const html = at('#shot-inspector').innerHTML;
      const rows = html.split('<button type="button" class="take-row').slice(1)
        .map((part) => '<button type="button" class="take-row' + part.split('</button>')[0]);
      console.log(JSON.stringify({{ rows, html }}));
    """)

    rows = drawn["rows"]
    assert len(rows) == 2, drawn["html"]
    # Every row is a button over its whole width, and the one the shot points at is the disabled
    # one. Nothing else in the strip is clickable, so there is no small live target beside a dead
    # line any more.
    assert '<div class="take-row' not in drawn["html"], (
        "a take row is a div again, which is the shape that could not be clicked"
    )
    assert 'class="quiet-button use-take"' not in drawn["html"], (
        "the small chip button is back; the row itself is supposed to be the control"
    )
    selectable, current = rows
    assert 'data-output="shots/shot_a/take_1.mp4"' in selectable, selectable
    assert "disabled" not in selectable, (
        "the take the shot is pointed away from is drawn shut", selectable
    )
    assert 'data-output="shots/shot_a/take_2.mp4"' in current, current
    assert "disabled" in current, (
        "the row the shot already points at is live, so it invites a click that does nothing",
        current,
    )
    assert "current" in current.split(">")[0]
    # The handler's selector still matches every row, so `disabled` stays the one place that
    # decides which takes are selectable.
    assert selectable.count("use-take") == 1 and current.count("use-take") == 1, rows
    # The full path is on the row itself now -- it used to be on an inner span, which is not
    # where a pointer rests when the row is the control.
    assert 'title="shots/shot_a/take_1.mp4"' in selectable, selectable


def test_each_take_row_says_which_render_it_came_from():
    """Two takes of one shot differ only by a serial buried in a filename the row truncates to an
    ellipsis -- `…-h3-reference_00001-audio.mp4` against `…_00002-audio.mp4` -- so the Director
    choosing between them was choosing between two identical-looking lines. The job record already
    holds the two facts that separate them: the seed the render used, and when the take landed.

    `takesStripRows` carries both as raw values and formats neither. The time is a locale string
    and that is a rendering decision; the seed is a number. A row whose record carries neither
    draws no provenance line at all rather than an empty one or an `Invalid Date`."""
    jobs = [
        {"id": "job_1", "kind": "h3", "target_id": "shot_a", "status": "complete", "seed": 3,
         "output_files": ["shots/shot_a/take_1.mp4"],
         "created_at": "2026-08-20T09:15:00Z", "updated_at": "2026-08-20T09:21:00Z"},
        # No seed and no timestamps: a record written before those fields existed.
        {"id": "job_2", "kind": "h3", "target_id": "shot_a", "status": "complete",
         "output_files": ["shots/shot_a/take_2.mp4"]},
        # A seed of 0 is a seed a render can genuinely have used, so it must not read as unknown.
        {"id": "job_3", "kind": "h3", "target_id": "shot_a", "status": "complete", "seed": 0,
         "output_files": ["shots/shot_a/take_3.mp4"], "updated_at": "2026-08-20T12:00:00Z"},
    ]
    read = run_module(f"""
      import {{ takesStripRows }} from './src/music_video_producer/web/assets/api.js';
      const strip = takesStripRows(
        {{ jobs: {json.dumps(jobs)} }},
        {{ id: 'shot_a', status: 'complete', latest_output: 'shots/shot_a/take_1.mp4' }},
      );
      console.log(JSON.stringify(strip.rows.map(
        (row) => ({{ file: row.file, seed: row.seed, at: row.at }})
      )));
    """)

    assert read[0] == {"file": "shots/shot_a/take_1.mp4", "seed": 3,
                       "at": "2026-08-20T09:21:00Z"}, read[0]
    # `updated_at` is when the take landed; `created_at` is when the render was queued. The row
    # says when the file appeared.
    assert read[0]["at"] != "2026-08-20T09:15:00Z"
    # An old record loses the line rather than inventing one.
    assert read[1] == {"file": "shots/shot_a/take_2.mp4", "seed": None, "at": ""}, read[1]
    # Seed 0 is a seed.
    assert read[2]["seed"] == 0, read[2]

    # And what the panel writes from that: a provenance line, formatted here rather than in the
    # pure function, absent entirely when there is nothing to say.
    drawn = run_workspace(rf"""
      state.project = {{ id: 'p1', jobs: {json.dumps(jobs)}, assets: [], shots: [
        {{ id: 'shot_a', start: 0, duration: 4, prompt: 'a wolf', status: 'complete',
           latest_output: 'shots/shot_a/take_1.mp4' }},
      ] }};
      state.selectedShotId = 'shot_a';
      app.renderShotInspector();
      const html = at('#shot-inspector').innerHTML;
      const metas = [...html.matchAll(/<span class="take-meta">([^<]*)<\/span>/g)].map((m) => m[1]);
      console.log(JSON.stringify({{ metas, rowCount: html.split('take-row').length - 1 }}));
    """)

    assert drawn["rowCount"] == 3, drawn
    # Two lines for three rows: the record carrying neither a seed nor a time draws none.
    assert len(drawn["metas"]) == 2, drawn["metas"]
    assert drawn["metas"][0].startswith("seed 3"), drawn["metas"]
    assert "seed 0" in drawn["metas"][1], drawn["metas"]
    assert drawn["metas"][0] != drawn["metas"][1], (
        "two takes of one shot render the same provenance line", drawn["metas"]
    )


def test_the_take_row_and_the_current_row_are_styled_as_open_and_closed():
    """A control that looks identical whether it is live is the defect this row is the fix for, so
    the two states are separated in the stylesheet as well as in the markup: the selectable row
    gets a hover, and the disabled one loses the pointer. `button:focus-visible` in the base rules
    is what gives the row a visible focus ring once it is a real button."""
    styles = STYLES_CSS.read_text(encoding="utf-8")
    assert ".take-row:not(:disabled):hover" in styles, (
        "the selectable take row has no hover state, so it does not read as clickable"
    )
    assert ".take-row:disabled { cursor: default;" in styles, (
        "the current take row keeps a pointer cursor and so still invites a dead click"
    )
    for rule in (".take-row .take-chip", ".take-row .take-meta"):
        assert rule in styles, f"{rule} has no style of its own"
    # The name is what truncates; the provenance line beneath it is what stays distinguishing.
    assert ".take-row .take-name { grid-column: 1; overflow: hidden;" in styles
    assert "button:focus-visible" in styles, (
        "nothing in this stylesheet gives a focused button a visible ring, and the take row is "
        "now reached by keyboard"
    )


# ------------------------------------------------------------------------------------------
# Direct manipulation on the SHOTS track: undo/redo, the gap-fill double-click, and snapping a
# cut to the playhead. The Director's asks, 2026-08-21.
#
# Every decision is executed under node rather than read out of `app.js`. This file carries a
# recorded incident where substring assertions let three UI guarantees invert with a green
# suite, and every one of these is a rule whose *inverse* would still contain every string it
# is made of. The browser half -- that the gestures are bound, reachable and land on the
# server -- is `tests/e2e_timeline_edit.py`, which drives a real Edge.
# ------------------------------------------------------------------------------------------

#: A plan shaped like the Director's own: shots tiling a song, with the four sub-frame gaps
#: their real project carries (0.002 s, 0.004 s, 0.014 s, 0.015 s) plus one large one.
GAPPY_PLAN = """
const project = {
  id: 'p1', updated_at: 'rev-1',
  song: { duration: 40, path: 'songs/000-x.wav' },
  shots: [
    { id: 's1', start: 0, duration: 5, status: 'draft' },
    { id: 's2', start: 5.002, duration: 5, status: 'draft' },
    { id: 's3', start: 10.016, duration: 5, status: 'draft' },
    { id: 's4', start: 15.02, duration: 5, status: 'draft' },
    { id: 's5', start: 22, duration: 5, status: 'draft' },
    { id: 's6', start: 27, duration: 12.986, status: 'draft' },
  ],
};
"""


def test_the_assembly_tolerances_the_timeline_judges_contiguity_by_are_the_assemblers_own():
    """A second, drifting copy would let the timeline call a plan contiguous that assembly then
    refuses -- which is the worst possible place to learn the number is wrong."""
    from music_video_producer.assembly import (
        ASSEMBLY_FPS,
        BOUNDARY_TOLERANCE_SECONDS,
        COVERAGE_TOLERANCE_SECONDS,
    )

    numbers = run_module("""
      import { ASSEMBLY_FPS, BOUNDARY_TOLERANCE_SECONDS, COVERAGE_TOLERANCE_SECONDS }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        fps: ASSEMBLY_FPS, boundary: BOUNDARY_TOLERANCE_SECONDS, coverage: COVERAGE_TOLERANCE_SECONDS,
      }));
    """)
    assert numbers["fps"] == ASSEMBLY_FPS
    assert numbers["boundary"] == pytest.approx(BOUNDARY_TOLERANCE_SECONDS)
    assert numbers["coverage"] == pytest.approx(COVERAGE_TOLERANCE_SECONDS)


def test_the_cut_move_refusals_are_the_snap_routes_own_sentences_in_its_own_order():
    """Two gestures now move a cut from the client, and `timeline.cut_move_refusal` already
    rules who may. Reworded copies would be a second thing to keep true, and the Director reads
    whichever one happens to fire."""
    from music_video_producer.timeline import (
        SNAP_APPROVED_REFUSAL,
        SNAP_IN_FLIGHT_REFUSAL,
        SNAP_LOCKED_REFUSAL,
    )

    wordings = run_module("""
      import { CUT_APPROVED_REFUSAL, CUT_IN_FLIGHT_REFUSAL, CUT_LOCKED_REFUSAL, cutMoveRefusal }
        from './src/music_video_producer/web/assets/api.js';
      const project = { shots: [
        { id: 'a', start: 0, duration: 5, locked: true, approved_output: 'x', status: 'queued' },
        { id: 'b', start: 5, duration: 5, approved_output: 'takes/b.mp4', status: 'queued' },
        { id: 'c', start: 10, duration: 5, status: 'running' },
        { id: 'd', start: 15, duration: 5, status: 'complete' },
      ]};
      console.log(JSON.stringify({
        locked: CUT_LOCKED_REFUSAL, approved: CUT_APPROVED_REFUSAL, flight: CUT_IN_FLIGHT_REFUSAL,
        order: cutMoveRefusal(project, project.shots[0]),
        approvedShot: cutMoveRefusal(project, project.shots[1]),
        renderingShot: cutMoveRefusal(project, project.shots[2]),
        settled: cutMoveRefusal(project, project.shots[3]),
        missing: cutMoveRefusal(project, null),
      }));
    """)
    assert wordings["locked"] == SNAP_LOCKED_REFUSAL
    assert wordings["approved"] == SNAP_APPROVED_REFUSAL
    assert wordings["flight"] == SNAP_IN_FLIGHT_REFUSAL
    # All three apply to `a`; a lock is the Director's own decision, so it is the sentence worth
    # reading, exactly as it is on the server.
    assert wordings["order"] == SNAP_LOCKED_REFUSAL.format(shot="SHOT 01 (a)")
    assert wordings["approvedShot"] == SNAP_APPROVED_REFUSAL.format(shot="SHOT 02 (b)")
    assert wordings["renderingShot"] == SNAP_IN_FLIGHT_REFUSAL.format(shot="SHOT 03 (c)")
    # A rendered, unapproved shot's window is dragged every day of production. Gating on render
    # provenance would refuse most of a mid-production plan, which is `cut_move_refusal`'s own
    # ruling and has to stay true on this side too.
    assert wordings["settled"] == ""
    assert wordings["missing"] == ""


def test_a_double_click_closes_a_sub_frame_gap_as_readily_as_a_large_one():
    """The Director's plan carries 0.002 s, 0.004 s, 0.014 s and 0.015 s gaps -- residue from
    hand-dragging edges. Every one is far inside `BOUNDARY_TOLERANCE_SECONDS`, so a rule that
    only noticed gaps worth assembling about would decline every gap they actually have."""
    plans = run_module(GAPPY_PLAN + """
      import { contiguityProblems, gapFillPlan, planSeams }
        from './src/music_video_producer/web/assets/api.js';
      const fill = (id, edge) => gapFillPlan(project, id, edge);
      const applied = JSON.parse(JSON.stringify(project));
      // Every gap closed, one double-click at a time, with the plan carried forward between
      // them: a long chain of gestures is the case a single call cannot cover.
      for (const [id, edge] of [['s1','right'], ['s3','left'], ['s4','left'], ['s5','left'], ['s6','right']]) {
        const step = gapFillPlan(applied, id, edge);
        if (!step.ok) throw new Error(id + '/' + edge + ': ' + step.refusal);
        const shot = applied.shots.find((item) => item.id === id);
        shot.start = step.start;
        shot.duration = step.duration;
      }
      console.log(JSON.stringify({
        tiny: fill('s1', 'right'),
        tinyLeft: fill('s2', 'left'),
        large: fill('s5', 'left'),
        none: fill('s6', 'left'),
        tail: fill('s6', 'right'),
        head: fill('s1', 'left'),
        seamsAfter: planSeams(applied.shots, 40),
        problemsAfter: contiguityProblems(applied.shots, 40),
      }));
    """)
    # A 0.002 s gap closes, and the edge lands exactly on the neighbour rather than near it.
    assert plans["tiny"]["ok"] is True
    assert plans["tiny"]["gap"] == pytest.approx(0.002)
    assert plans["tiny"]["start"] == 0
    assert plans["tiny"]["duration"] == pytest.approx(5.002)
    assert plans["tiny"]["neighbourId"] == "s2"
    # The other edge of the same gap: s2's left edge runs back to meet s1's end.
    assert plans["tinyLeft"]["ok"] is True
    assert plans["tinyLeft"]["start"] == pytest.approx(5.0)
    assert plans["tinyLeft"]["duration"] == pytest.approx(5.002)
    # A large gap is the same gesture with a bigger number, not a different rule.
    assert plans["large"]["ok"] is True
    assert plans["large"]["gap"] == pytest.approx(1.98)
    # An edge that already meets its neighbour has nothing to close, and says so by name.
    assert plans["none"]["ok"] is False
    assert "SHOT 06 (s6)" in plans["none"]["refusal"]
    assert "no gap" in plans["none"]["refusal"]
    # The song's own head and tail are the boundary when there is no shot there. The last clip
    # ends at 39.986 in a 40 s song, so its right edge has 0.014 s of song to reach.
    assert plans["tail"]["ok"] is True
    assert plans["tail"]["gap"] == pytest.approx(0.014)
    assert plans["tail"]["against"] == "the end of the song"
    # The first clip already starts at 0, so there is no head gap to close.
    assert plans["head"]["ok"] is False
    # And the chain of five gestures leaves a plan that tiles the song exactly.
    assert plans["problemsAfter"] == []
    assert [round(seam["seconds"], 9) for seam in plans["seamsAfter"]] == [0, 0, 0, 0, 0, 0, 0]


def test_the_gap_fill_never_swallows_an_overlapping_neighbour():
    """The dangerous shape: an edge that already overlaps the next clip. Searching for "the
    nearest neighbour that does not overlap" would skip the clip underneath and stretch to the
    one after it -- taking a shot out of the picture without taking it out of the plan."""
    verdicts = run_module("""
      import { gapFillPlan } from './src/music_video_producer/web/assets/api.js';
      const project = { song: { duration: 30 }, shots: [
        { id: 'a', start: 0, duration: 12, status: 'draft' },
        { id: 'b', start: 10, duration: 5, status: 'draft' },
        { id: 'c', start: 20, duration: 10, status: 'draft' },
      ]};
      console.log(JSON.stringify({
        overlapping: gapFillPlan(project, 'a', 'right'),
        realGap: gapFillPlan(project, 'b', 'right'),
      }));
    """)
    assert verdicts["overlapping"]["ok"] is False, (
        "an already-overlapping edge was offered a stretch that would have swallowed SHOT 02"
    )
    assert verdicts["realGap"]["ok"] is True
    assert verdicts["realGap"]["neighbourId"] == "c"
    assert verdicts["realGap"]["duration"] == pytest.approx(10)


def test_the_gap_fill_refuses_on_a_protected_shot_or_a_protected_neighbour():
    """A cut belongs to the two shots that share it, so both are asked -- which is
    `snap_cut_plan`'s own rule rather than a second, narrower one invented here."""
    from music_video_producer.timeline import SNAP_APPROVED_REFUSAL, SNAP_LOCKED_REFUSAL

    verdicts = run_module("""
      import { gapFillPlan } from './src/music_video_producer/web/assets/api.js';
      const plan = (extra) => ({ song: { duration: 30 }, shots: [
        { id: 'a', start: 0, duration: 5, status: 'draft', ...(extra.a || {}) },
        { id: 'b', start: 6, duration: 5, status: 'draft', ...(extra.b || {}) },
      ]});
      console.log(JSON.stringify({
        open: gapFillPlan(plan({}), 'a', 'right'),
        lockedSelf: gapFillPlan(plan({ a: { locked: true } }), 'a', 'right'),
        approvedNeighbour: gapFillPlan(plan({ b: { approved_output: 'takes/b.mp4' } }), 'a', 'right'),
        renderingNeighbour: gapFillPlan(plan({ b: { status: 'queued' } }), 'a', 'right'),
      }));
    """)
    assert verdicts["open"]["ok"] is True
    assert verdicts["lockedSelf"]["ok"] is False
    assert verdicts["lockedSelf"]["refusal"] == SNAP_LOCKED_REFUSAL.format(shot="SHOT 01 (a)")
    assert verdicts["approvedNeighbour"]["ok"] is False
    assert verdicts["approvedNeighbour"]["refusal"] == SNAP_APPROVED_REFUSAL.format(
        shot="SHOT 02 (b)"
    )
    assert verdicts["renderingNeighbour"]["ok"] is False


def test_the_playhead_magnet_is_measured_in_pixels_so_it_feels_the_same_at_every_zoom():
    """A tolerance in seconds would swallow a whole short shot at 6 px/s and be unreachably
    fine at 64 px/s. It also declines while the song is playing: a moving playhead is not
    something an edge can be lined up against."""
    pulls = run_module("""
      import { PLAYHEAD_SNAP_PIXELS, playheadSnap } from './src/music_video_producer/web/assets/api.js';
      const at = (extra) => playheadSnap({ seconds: 12.3, playhead: 12, pixelsPerSecond: 16, ...extra });
      console.log(JSON.stringify({
        pixels: PLAYHEAD_SNAP_PIXELS,
        near: at({}),
        zoomedIn: at({ pixelsPerSecond: 64 }),
        zoomedOut: at({ pixelsPerSecond: 6 }),
        far: playheadSnap({ seconds: 13.5, playhead: 12, pixelsPerSecond: 16 }),
        off: at({ enabled: false }),
        playing: at({ playing: true }),
        noScale: at({ pixelsPerSecond: 0 }),
      }));
    """)
    assert pulls["pixels"] == 8
    # 0.3 s away: inside 8 px at 16 px/s (4.8 px), outside it at 64 px/s (19.2 px).
    assert pulls["near"] == {"snapped": True, "seconds": 12}
    assert pulls["zoomedIn"]["snapped"] is False
    assert pulls["zoomedIn"]["seconds"] == pytest.approx(12.3)
    assert pulls["zoomedOut"]["snapped"] is True
    assert pulls["far"]["snapped"] is False
    assert pulls["off"]["snapped"] is False
    assert pulls["playing"]["snapped"] is False, (
        "an edge snapped to a playhead that is running, which is a moving target"
    )
    assert pulls["noScale"]["snapped"] is False


def test_snapping_a_cut_moves_the_shared_boundary_and_leaves_the_plan_contiguous():
    """The hard constraint. A shot's edge is its neighbour's edge, so both windows change --
    the freehand drag changes one, which is how the Director's plan came to hold four sub-frame
    gaps in the first place."""
    moves = run_module("""
      import { boundaryMovePlan, contiguityProblems }
        from './src/music_video_producer/web/assets/api.js';
      const project = { song: { duration: 30 }, shots: [
        { id: 'a', start: 0, duration: 10, status: 'draft' },
        { id: 'b', start: 10, duration: 10, status: 'draft' },
        { id: 'c', start: 20, duration: 10, status: 'draft' },
      ]};
      const right = boundaryMovePlan(project, 'a', 'right', 12.5);
      const applied = JSON.parse(JSON.stringify(project));
      for (const w of right.windows) {
        const shot = applied.shots.find((item) => item.id === w.id);
        shot.start = w.start; shot.duration = w.duration;
      }
      console.log(JSON.stringify({
        right,
        left: boundaryMovePlan(project, 'b', 'left', 12.5),
        after: contiguityProblems(applied.shots, 30),
        unshared: boundaryMovePlan(
          { shots: [{ id: 'x', start: 0, duration: 5 }, { id: 'y', start: 8, duration: 5 }] },
          'x', 'right', 6,
        ),
        collapsed: boundaryMovePlan(project, 'a', 'right', 20),
      }));
    """)
    assert moves["right"]["ok"] is True
    assert moves["right"]["sharedId"] == "b"
    assert moves["right"]["windows"] == [
        {"id": "a", "start": 0, "duration": 12.5},
        {"id": "b", "start": 12.5, "duration": 7.5},
    ]
    # Grabbing the same cut from its other side is the same move.
    assert sorted(moves["left"]["windows"], key=lambda w: w["start"]) == sorted(
        moves["right"]["windows"], key=lambda w: w["start"]
    )
    assert moves["after"] == [], "snapping a cut left a gap or an overlap in the plan"
    # A neighbour on the far side of a real gap is not sharing this cut; that is what the
    # double-click is for, and closing it silently here would be a second gesture in disguise.
    assert moves["unshared"]["ok"] is True
    assert moves["unshared"]["sharedId"] is None
    assert len(moves["unshared"]["windows"]) == 1
    # Zero length refuses; nothing here forbids a merely *short* window.
    assert moves["collapsed"]["ok"] is False
    assert "no length at all" in moves["collapsed"]["refusal"]


def test_snapping_a_cut_never_refuses_a_window_for_being_short():
    """Short windows are legitimate and are deliberately being made more so. A minimum-length
    rule invented on this side would forbid, from the client, exactly what the render path is
    being taught to allow."""
    verdict = run_module("""
      import { boundaryMovePlan } from './src/music_video_producer/web/assets/api.js';
      const project = { shots: [
        { id: 'a', start: 0, duration: 10, status: 'draft' },
        { id: 'b', start: 10, duration: 10, status: 'draft' },
      ]};
      console.log(JSON.stringify({
        short: boundaryMovePlan(project, 'a', 'right', 0.25),
        shorterStill: boundaryMovePlan(project, 'a', 'right', 0.04),
      }));
    """)
    # A quarter of a second: far under any shot-length band this application has ever had.
    assert verdict["short"]["ok"] is True
    assert verdict["short"]["windows"][0]["duration"] == pytest.approx(0.25)
    assert verdict["shorterStill"]["ok"] is True


def test_the_undo_button_says_what_it_would_undo_and_shuts_when_the_project_moved():
    """The whole safety rule, executed. A stack that replayed over a revision it does not
    account for is the one thing this feature must not do -- it would revert a landed render."""
    from music_video_producer.app import PROJECT_CHANGED_REFUSAL

    states = run_module("""
      import { ASSISTANT_EDIT_BLOCKED, PROJECT_CHANGED_REFUSAL, SHOT_EXPANSION_EDIT_BLOCKED,
               UNDO_DEPTH, undoControl, undoGestureLabel }
        from './src/music_video_producer/web/assets/api.js';
      const stack = [{ kind: 'move' }, { kind: 'split' }];
      console.log(JSON.stringify({
        refusal: PROJECT_CHANGED_REFUSAL,
        depth: UNDO_DEPTH,
        empty: undoControl([], { revision: 'r1', projectRevision: 'r1' }),
        emptyRedo: undoControl([], { revision: 'r1', projectRevision: 'r1', redo: true }),
        ready: undoControl(stack, { revision: 'r1', projectRevision: 'r1' }),
        readyRedo: undoControl(stack, { revision: 'r1', projectRevision: 'r1', redo: true }),
        moved: undoControl(stack, { revision: 'r1', projectRevision: 'r2' }),
        unknown: undoControl(stack, { revision: null, projectRevision: 'r2' }),
        expanding: undoControl(stack, { revision: 'r1', projectRevision: 'r1', busy: 'expansion' }),
        filling: undoControl(stack, { revision: 'r1', projectRevision: 'r1', busy: 'assistant' }),
        blocked: SHOT_EXPANSION_EDIT_BLOCKED,
        assistantBlocked: ASSISTANT_EDIT_BLOCKED,
        unknownGesture: undoGestureLabel('something-new'),
      }));
    """)
    assert states["refusal"] == PROJECT_CHANGED_REFUSAL
    assert states["depth"] == 40
    assert states["empty"]["disabled"] is True
    assert "Nothing to undo" in states["empty"]["title"]
    assert states["emptyRedo"]["disabled"] is True
    assert states["emptyRedo"]["title"] != states["empty"]["title"]
    # It names the gesture. "Undo" alone does not say what comes back, which is the whole point.
    assert states["ready"]["disabled"] is False
    assert "the split" in states["ready"]["title"]
    assert "Ctrl+Z" in states["ready"]["title"]
    assert "Ctrl+Shift+Z" in states["readyRedo"]["title"]
    # The server's own sentence, said before the request rather than only after it.
    assert states["moved"]["disabled"] is True
    assert states["moved"]["title"].startswith(PROJECT_CHANGED_REFUSAL)
    # A revision nobody knows is treated as a revision that moved. Refusing is recoverable;
    # replaying over an unknown state is not.
    assert states["unknown"]["disabled"] is True
    # And the two automated writes hold it shut in their own words rather than a third.
    assert states["expanding"]["title"] == states["blocked"]
    assert states["filling"]["title"] == states["assistantBlocked"]
    assert states["unknownGesture"] == "the last shot edit"


def test_the_undo_write_carries_the_stacks_revision_and_the_pre_gesture_shots():
    """Driven end to end against the stub DOM: split a shot, let the save land, press Undo, and
    read what went on the wire. A source read cannot tell a stack that records the state *after*
    a gesture from one that records the state before it, and the two differ by everything."""
    driven = run_workspace(
        """
        state.project = {
          id: 'p1', updated_at: 'rev-1', name: 'x', assets: [], jobs: [], messages: [],
          sections: [], song: { duration: 20, path: 'songs/000-x.wav' },
          shots: [
            { id: 's1', start: 0, duration: 10, prompt: 'one', status: 'draft', citations: [] },
            { id: 's2', start: 10, duration: 10, prompt: 'two', status: 'draft', citations: [] },
          ],
        };
        state.selectedShotId = 's1';
        // What every project load does before a Director can click anything: draw the timeline
        // once, which is where the history takes its baseline from the plan on screen.
        app.syncUndoControls();
        fire('#split-shot:click', {});
        await flush();
        const split = requests.filter((entry) => entry.method === 'PUT').at(-1);
        const shotsAfterSplit = state.project.shots.length;
        const undoBefore = { title: at('#undo-shots').title, disabled: at('#undo-shots').disabled };
        fire('#undo-shots:click', {});
        await flush();
        const undone = requests.filter((entry) => entry.method === 'PUT').at(-1);
        const redoAfter = at('#redo-shots');
        console.log(JSON.stringify({
          split: JSON.parse(split.body),
          shotsAfterSplit,
          undoTitle: undoBefore.title,
          undoDisabled: undoBefore.disabled,
          undo: JSON.parse(undone.body),
          shotsAfterUndo: state.project.shots.length,
          redoDisabled: redoAfter.disabled,
          redoTitle: redoAfter.title,
        }));
        """,
        responses={
            # The split's own save, and the undo write after it. Each answers with the shots the
            # route holds and a fresh revision, exactly as `PUT /shots` does.
            "/api/projects/p1/shots": {
                "body": {
                    "id": "p1",
                    "updated_at": "rev-2",
                    "shots": [
                        {"id": "s1", "start": 0, "duration": 10, "status": "draft"},
                        {"id": "s2", "start": 10, "duration": 10, "status": "draft"},
                    ],
                }
            },
        },
    )
    # The split really happened, and it was sent whole.
    assert driven["shotsAfterSplit"] == 3
    assert len(driven["split"]["shots"]) == 3
    assert driven["split"]["updated_at"] == "rev-1"
    # The button names the gesture once the save has landed.
    assert driven["undoDisabled"] is False
    assert "the split" in driven["undoTitle"]
    # And the undo carries the *pre-split* list, against the revision the save produced.
    assert len(driven["undo"]["shots"]) == 2, driven["undo"]
    assert [shot["duration"] for shot in driven["undo"]["shots"]] == [10, 10]
    assert driven["undo"]["updated_at"] == "rev-2", (
        "the undo was sent against the revision this client loaded rather than the one its own "
        "save produced, so it would 409 on every plan that has been edited once"
    )
    # The reply is adopted, so the screen shows what the server holds rather than what the
    # client hoped for -- and redo is now offered.
    assert driven["shotsAfterUndo"] == 2
    assert driven["redoDisabled"] is False
    assert "the split" in driven["redoTitle"]


def test_a_refused_save_records_nothing_to_undo():
    """An undo of something that was never applied is the case that must not exist. The entry is
    created when the server confirms the write and at no other moment, so a refused gesture
    leaves the button exactly as shut as it was."""
    driven = run_workspace(
        """
        state.project = {
          id: 'p2', updated_at: 'rev-1', name: 'x', assets: [], jobs: [], messages: [],
          sections: [], song: { duration: 20, path: 'songs/000-x.wav' },
          shots: [
            { id: 's1', start: 0, duration: 10, prompt: 'one', status: 'draft', citations: [] },
            { id: 's2', start: 10, duration: 10, prompt: 'two', status: 'draft', citations: [] },
          ],
        };
        state.selectedShotId = 's1';
        // What every project load does before a Director can click anything: draw the timeline
        // once, which is where the history takes its baseline from the plan on screen.
        app.syncUndoControls();
        fire('#split-shot:click', {});
        await flush();
        fire('#undo-shots:click', {});
        await flush();
        console.log(JSON.stringify({
          shots: state.project.shots.length,
          undoDisabled: at('#undo-shots').disabled,
          undoTitle: at('#undo-shots').title,
          writes: requests.filter((entry) => entry.method === 'PUT').length,
        }));
        """,
        responses={
            "/api/projects/p2/shots": {
                "status": 409,
                "body": {
                    "detail": "Project changed since it was loaded; refresh before replacing it"
                },
            },
        },
    )
    # One write went out -- the split's, which was refused. The undo click sent nothing.
    assert driven["writes"] == 1, driven
    assert driven["undoDisabled"] is True
    assert "Nothing to undo" in driven["undoTitle"]


def test_expand_all_prompts_is_offered_on_the_cuts_bar_with_the_sweeps_own_words():
    """The Director's fourth ask: the control "up by where the Cuts and Snap Cuts stuff are".
    A second affordance for one route -- so it shares the route, the refusal and the help, and
    the timeline's copy is drawn by `renderSnapCuts` rather than shipped in the markup."""
    drawn = run_workspace("""
      const base = {
        id: 'p3', updated_at: 'r1', name: 'x', assets: [], jobs: [], sections: [], messages: [],
        song: { duration: 20, path: 'songs/000-x.wav', lyric_words: [], vocal_spans: [] },
      };
      state.project = { ...base, shots: [] };
      app.renderSnapCuts();
      const empty = at('#snap-bar').innerHTML;
      state.project = { ...base, shots: [{ id: 's1', start: 0, duration: 20, status: 'draft' }] };
      app.renderSnapCuts();
      const planned = at('#snap-bar').innerHTML;
      console.log(JSON.stringify({
        selector: contract.EXPAND_ALL_PROMPTS_TIMELINE_CONTROL,
        label: contract.EXPAND_ALL_PROMPTS_TIMELINE_LABEL,
        help: contract.EXPAND_ALL_PROMPTS_HELP,
        refusal: contract.EXPAND_ALL_PROMPTS_WITHOUT_SHOTS,
        empty, planned,
      }));
    """)
    # Its own id, because two elements may not share one -- the Director workspace keeps
    # `#expand-h3-prompts` and this is a second door to the same room.
    assert drawn["selector"] == "#timeline-expand-prompts"
    assert drawn["selector"] != "#expand-h3-prompts"
    for markup in (drawn["empty"], drawn["planned"]):
        assert 'id="timeline-expand-prompts"' in markup
        assert drawn["label"] in markup
    # A plan with no shots draws it shut, saying the route's own sentence.
    assert "disabled" in drawn["empty"].split('id="timeline-expand-prompts"')[1].split(">")[0]
    assert drawn["refusal"][:40] in drawn["empty"]
    # A plan with shots draws it live, saying what pressing it costs before the click.
    assert "disabled" not in drawn["planned"].split('id="timeline-expand-prompts"')[1].split(">")[0]
    assert "one call per shot" in drawn["planned"]


def test_the_sweeps_per_shot_report_is_drawn_beside_the_button_that_raised_it():
    """The route answers per shot on purpose: "a locked shot the sweep silently skipped is
    indistinguishable to the Director from one it forgot". Those notices live in the Director
    thread, which is two panels away from this button, so they are drawn here as well -- whole,
    labelled by kind in words, and never summarised."""
    reply = (
        "The specialist ran once per shot.\n\n---\n"
        "H3 prompts written for 2 shot(s): SHOT 01, SHOT 03\n\n"
        "SHOT 02 is locked and was not touched.\n\n"
        "SHOT 04 returned prose the format checker rejected."
    )
    drawn = run_module(
        f"const reply = {json.dumps(reply)};"
        """
      import { expansionSweepLines } from './src/music_video_producer/web/assets/api.js';
      const project = { messages: [
        { id: 'm0', role: 'user', content: 'ignore me' },
        { id: 'm1', role: 'assistant', content: reply, notices: [
          { kind: 'change', text: 'H3 prompts written for 2 shot(s): SHOT 01, SHOT 03' },
          { kind: 'refusal', text: 'SHOT 02 is locked and was not touched.' },
          { kind: 'flag', text: 'SHOT 04 returned prose the format checker rejected.' },
        ] },
      ]};
      console.log(JSON.stringify({ lines: expansionSweepLines(project) }));
        """
    )
    assert [line["kind"] for line in drawn["lines"]] == ["change", "refusal", "flag"]
    assert "SHOT 02 is locked" in drawn["lines"][1]["text"]
    assert "format checker rejected" in drawn["lines"][2]["text"]
    # And every one of them reaches the bar, in its own words and under its own label.
    markup = run_workspace(
        f"const reply = {json.dumps(reply)};"
        """
      state.project = {
        id: 'p4', updated_at: 'r1', name: 'x', assets: [], jobs: [], sections: [],
        song: { duration: 20, path: 'songs/000-x.wav', lyric_words: [], vocal_spans: [] },
        shots: [{ id: 's1', start: 0, duration: 20, status: 'draft' }],
        messages: [{ id: 'm1', role: 'assistant', content: reply, notices: [
          { kind: 'change', text: 'H3 prompts written for 2 shot(s): SHOT 01, SHOT 03' },
          { kind: 'refusal', text: 'SHOT 02 is locked and was not touched.' },
          { kind: 'flag', text: 'SHOT 04 returned prose the format checker rejected.' },
        ] }],
      };
      app.recordExpansionSweepReport();
      app.renderSnapCuts();
      console.log(JSON.stringify({ bar: at('#snap-bar').innerHTML }));
        """
    )
    for sentence in (
        "H3 prompts written for 2 shot(s): SHOT 01, SHOT 03",
        "SHOT 02 is locked and was not touched.",
        "SHOT 04 returned prose the format checker rejected.",
    ):
        assert escape_html(sentence) in markup["bar"], (
            f"the sweep report swallowed {sentence!r}, which is the half a Director has to read"
        )
    for label in ("Change applied", "Safety notice", "Check this"):
        assert label in markup["bar"], f"{label} is not on the report, so the kind is colour alone"


def escape_html(value: str) -> str:
    """api.js's `escapeHtml`, for asserting about markup it produced."""
    for char, entity in (
        ("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;")
    ):
        value = value.replace(char, entity)
    return value


def test_the_timeline_tools_carry_undo_redo_and_the_playhead_magnet():
    """In the bar the Director was told to look in, with an accessible name each. A tooltip is
    not an accessible name and a glyph is not a label -- the rule that produced "i see what i
    think is a zoom slider that isnt functional"."""
    markup = INDEX_HTML.read_text(encoding="utf-8")
    tools = re.search(r'<div class="timeline-tools">.*?</div>', markup, re.DOTALL)
    assert tools, "the bar under the Monitor no longer has a tools group"
    for control in ("undo-shots", "redo-shots", "snap-playhead"):
        button = re.search(rf'<button[^>]*id="{control}"[^>]*>', tools.group(0))
        assert button, f"#{control} is not in the bar under the Monitor"
        assert "aria-label=" in button.group(0), button.group(0)
    # Both history buttons ship shut: the stack is empty until a save lands, and a live-looking
    # button before the first edit is a promise this feature deliberately does not make.
    for control in ("undo-shots", "redo-shots"):
        assert "disabled" in re.search(rf'<button[^>]*id="{control}"[^>]*>', markup).group(0)
    # The magnet is a toggle, so its state is `aria-pressed` and not a class alone.
    assert 'aria-pressed="true"' in re.search(
        r'<button[^>]*id="snap-playhead"[^>]*>', markup
    ).group(0)


def test_ctrl_z_is_bound_and_is_not_swallowed_by_the_transport_keys():
    """Guarded off editable elements by the same check the transport keys use, so Ctrl+Z inside
    a prompt textarea is still the browser's own text undo -- and returning before the transport
    branch, so the combination cannot fall through into a one-frame seek."""
    handler = app_js_block('document.addEventListener?.("keydown"', "\n  });")

    assert 'event.target.matches?.("input, textarea, select")' in handler
    assert "runUndo()" in handler and "runRedo()" in handler
    assert handler.index("runUndo()") < handler.index('event.code === "Space"'), (
        "the undo shortcut is decided after the transport keys, so Ctrl+Z can reach a seek"
    )
    assert "event.shiftKey" in handler


# ------------------------------------------------------------------------------------------
# The shot-length band on the clip. The Director's ruling, 2026-08-20: "I dont anticipate a shot
# being requested over 15 seconds, when dragging a clip past that it should turn yellow but we
# arent dead yet."
#
# The band's constants live in `timeline.py` and the short end's floor fires well below its
# nominal minimum, so the client reads the *server's verdict* rather than any constant: a
# re-derived band would paint a clip yellow the server considers fine, or leave one plain that
# it does not. These tests hold the browser to the report's own `kind`.
# ------------------------------------------------------------------------------------------


def test_the_clip_colours_from_the_reports_kind_and_never_from_its_own_arithmetic():
    """`window_short` is not a problem: the render floors at H3's minimum and centres the window
    inside it, so the exposed cut is exactly the window and a micro-cut is legitimate. Only the
    long end draws a state, and it draws words beside the colour."""
    from music_video_producer.batch import NOTE_KIND_WINDOW_LONG, NOTE_KIND_WINDOW_SHORT

    drawn = run_module("""
      import { CLIP_WINDOW_LONG_CLASS, NOTE_KIND_WINDOW_LONG, NOTE_KIND_WINDOW_SHORT,
               clipWindowState, windowWarningsByShot }
        from './src/music_video_producer/web/assets/api.js';
      const report = { window_warnings: [
        { shot_ids: ['long'], labels: ['SHOT 01 (long)'], reason: 'past the band', kind: 'window_long' },
        { shot_ids: ['short'], labels: ['SHOT 02 (short)'], reason: 'under the band', kind: 'window_short' },
      ]};
      console.log(JSON.stringify({
        long: NOTE_KIND_WINDOW_LONG, short: NOTE_KIND_WINDOW_SHORT,
        cls: CLIP_WINDOW_LONG_CLASS,
        byShot: windowWarningsByShot(report),
        none: windowWarningsByShot(null),
        drawnLong: clipWindowState('window_long'),
        drawnShort: clipWindowState('window_short'),
        drawnUnknown: clipWindowState(undefined),
        labelledLong: clipWindowState('window_long', 'A corridor push-in.').label,
        labelledShort: clipWindowState('window_short', 'A micro cut.').label,
      }));
    """)
    assert drawn["long"] == NOTE_KIND_WINDOW_LONG
    assert drawn["short"] == NOTE_KIND_WINDOW_SHORT
    assert drawn["byShot"] == {"long": "window_long", "short": "window_short"}
    assert drawn["none"] == {}
    # The long end: a class *and* a sentence. Colour alone carries no state here.
    assert drawn["drawnLong"]["className"] == "window-long"
    assert drawn["drawnLong"]["note"]
    assert "still submits and renders" in drawn["drawnLong"]["note"]
    # And it must not promise the extension that does not exist: the LTX graph is built and
    # audited, and nothing in this application submits it.
    assert "exten" not in drawn["drawnLong"]["note"].lower(), drawn["drawnLong"]["note"]
    # The short end draws nothing at all, and leaves the clip's accessible name untouched.
    assert drawn["drawnShort"] == {"className": "", "note": "", "label": ""}
    assert drawn["drawnUnknown"] == {"className": "", "note": "", "label": ""}
    assert drawn["labelledShort"] == "A micro cut."
    assert drawn["labelledLong"].startswith("A corridor push-in. — ")
    assert drawn["drawnLong"]["note"] in drawn["labelledLong"]


def test_the_window_notes_are_never_drawn_as_near_duplicates_or_counted_as_pairs():
    """`warnings` has exactly one meaning to every reader it already has -- "Near-duplicate", and
    "N near-duplicate pairs" in the summary. A window note folded into it would reach the
    Director under a name that is not what it says."""
    lines = run_module("""
      import { READINESS_SAMENESS_LABEL, READINESS_WINDOW_LONG_LABEL,
               READINESS_WINDOW_SHORT_LABEL, readinessLines, readinessSummary }
        from './src/music_video_producer/web/assets/api.js';
      const report = {
        ready: true, shot_count: 3, ready_count: 3, blocking: [],
        warnings: [{ shot_ids: ['a', 'b'], labels: ['SHOT 01 (a)', 'SHOT 02 (b)'],
                     reason: 'These two shots share one prompt.', kind: 'sameness' }],
        warnings_computed: true, warnings_omitted: 0,
        window_warnings: [
          { shot_ids: ['c'], labels: ['SHOT 03 (c)'], reason: 'This shot is 20.000s, past the band. This does not block submission.', kind: 'window_long' },
          { shot_ids: ['a'], labels: ['SHOT 01 (a)'], reason: 'This shot is 1.000s, under the band. This does not block submission.', kind: 'window_short' },
        ],
      };
      console.log(JSON.stringify({
        lines: readinessLines(report),
        summary: readinessSummary(report),
        sameness: READINESS_SAMENESS_LABEL,
        longLabel: READINESS_WINDOW_LONG_LABEL,
        shortLabel: READINESS_WINDOW_SHORT_LABEL,
      }));
    """)
    kinds = [line["kind"] for line in lines["lines"]]
    assert kinds == ["warning", "window-long", "window-short"], kinds
    # Each under its own name, and the two window lines never under the sameness one.
    assert lines["lines"][0]["text"].startswith(lines["sameness"])
    assert lines["lines"][1]["text"].startswith(lines["longLabel"])
    assert lines["lines"][2]["text"].startswith(lines["shortLabel"])
    for line in lines["lines"][1:]:
        assert lines["sameness"] not in line["text"], line["text"]
    # The server's sentence, passed through whole -- including the half that says it is not a
    # block, which is the half a Director needs in order to carry on.
    assert "does not block submission" in lines["lines"][1]["reason"]
    # And the summary still counts one pair, not three.
    assert "1 near-duplicate pair" in lines["summary"], lines["summary"]
    assert "3 near-duplicate" not in lines["summary"], lines["summary"]


def test_the_window_verdict_the_browser_reads_is_the_one_the_route_answers_with():
    """End to end over a real report: the route's payload, fed to the client's own reader. A
    field renamed on either side is the failure this catches, and it is the failure that would
    otherwise leave every clip plain for ever with the whole suite green."""
    from music_video_producer.batch import readiness_report

    project = Project(
        name="Band",
        shots=[
            Shot(id="long", start=0, duration=20, prompt="A long corridor push-in.", mode="text"),
            Shot(id="fine", start=20, duration=8, prompt="A steady mid on the singer.", mode="text"),
            Shot(id="short", start=28, duration=1, prompt="A micro cut of the hands.", mode="text"),
        ],
    )
    report = readiness_report(project)
    payload = json.loads(json.dumps(asdict(report)))
    verdicts = run_module(
        f"const report = {json.dumps(payload)};"
        """
      import { clipWindowState, windowWarningsByShot }
        from './src/music_video_producer/web/assets/api.js';
      const byShot = windowWarningsByShot(report);
      console.log(JSON.stringify({
        byShot,
        long: clipWindowState(byShot.long, 'A long corridor push-in.'),
        fine: clipWindowState(byShot.fine, 'A steady mid.'),
        short: clipWindowState(byShot.short, 'A micro cut of the hands.'),
      }));
        """
    )
    assert verdicts["byShot"] == {"long": "window_long", "short": "window_short"}
    assert verdicts["long"]["className"] == "window-long"
    assert verdicts["fine"] == {"className": "", "note": "", "label": "A steady mid."}
    assert verdicts["short"] == {
        "className": "", "note": "", "label": "A micro cut of the hands."
    }


def test_the_long_window_clip_is_a_warning_and_not_a_block_anywhere():
    """The Director's ruling is that this warns and never refuses. Read as a scan rather than a
    behaviour test because it is a *negative*: what must not exist is a branch anywhere in the
    client that reads the band and shuts something."""
    api = API_JS.read_text(encoding="utf-8")
    app = APP_JS.read_text(encoding="utf-8")
    # The band's kinds are read in exactly the two places that draw: the readiness list and the
    # clip. Nothing that decides `disabled`, a refusal or a submission may mention them.
    for source, name in ((api, "api.js"), (app, "app.js")):
        for line in source.splitlines():
            if "window_long" not in line and "WINDOW_LONG" not in line:
                continue
            assert "disabled" not in line, f"{name} shuts a control from the window band: {line}"
            assert "refus" not in line.lower() or line.strip().startswith("//"), (
                f"{name} refuses something from the window band: {line}"
            )
    # And the styles draw the long end and deliberately leave the short end alone.
    styles = STYLES_CSS.read_text(encoding="utf-8")
    assert ".shot-clip.window-long" in styles
    assert ".shot-clip.window-short" not in styles, (
        "the short end is drawn as a state on the clip, which says 'wrong' about a window the "
        "render handles by design"
    )


# --------------------------------------------------------------------------------------------
# The window against its own take -- the Director's second yellow, 2026-08-21: "if the bounds of
# the shots window are dragged beyond where that clip covers then the shot would turn yellow to
# warn that the bounds was gone past."
#
# It composes with the band rather than inventing a second mechanism: the same `window_warnings`
# list, the same `windowWarningsByShot` reader, the same amber, one more `kind`.
# --------------------------------------------------------------------------------------------


def test_the_uncovered_take_draws_the_bands_amber_under_its_own_class_and_sentence():
    """A second yellow the Director would have to learn as a second colour is the one thing this
    must not be. Same amber, own class, own words -- and the words are what a screen reader gets,
    so the class alone would be no state at all."""
    from music_video_producer.batch import NOTE_KIND_TAKE_UNCOVERED

    drawn = run_module("""
      import { CLIP_TAKE_UNCOVERED_CLASS, NOTE_KIND_TAKE_UNCOVERED, clipWindowState,
               windowWarningsByShot }
        from './src/music_video_producer/web/assets/api.js';
      const report = { window_warnings: [
        { shot_ids: ['past'], labels: ['SHOT 03 (past)'], reason: 'off the end', kind: 'take_uncovered' },
      ]};
      console.log(JSON.stringify({
        kind: NOTE_KIND_TAKE_UNCOVERED,
        cls: CLIP_TAKE_UNCOVERED_CLASS,
        byShot: windowWarningsByShot(report),
        drawn: clipWindowState('take_uncovered'),
        labelled: clipWindowState('take_uncovered', 'A corridor push-in.').label,
      }));
    """)
    assert drawn["kind"] == NOTE_KIND_TAKE_UNCOVERED
    assert drawn["byShot"] == {"past": "take_uncovered"}
    assert drawn["drawn"]["className"] == drawn["cls"] == "take-uncovered"
    assert drawn["drawn"]["note"]
    # It says nothing was stopped, because nothing was: the Director ruled that this colours and
    # never constrains, and a sentence that implied a refusal would undo the ruling in words.
    assert "Nothing is stopped" in drawn["drawn"]["note"]
    assert drawn["labelled"].startswith("A corridor push-in. — ")
    assert drawn["drawn"]["note"] in drawn["labelled"]
    # The same amber as the band, and the same two marks, in the stylesheet the Director sees.
    styles = STYLES_CSS.read_text(encoding="utf-8")
    assert ".shot-clip.take-uncovered { border-top: 2px solid var(--amber); }" in styles
    assert ".shot-clip.take-uncovered .clip-id { color: var(--amber); }" in styles
    assert ".plan-readiness li.take-uncovered { color: var(--amber); }" in styles


def test_a_shot_that_is_both_long_and_uncovered_wears_the_coverage_state_and_reads_both_lines():
    """Two notes, one clip. The clip has one border and one accessible name, so a precedence has
    to exist and be *decided* rather than inherited from whichever check the server ran first --
    and the readiness list still prints both sentences, because neither fact stops being true."""
    verdicts = run_module("""
      import { READINESS_TAKE_UNCOVERED_LABEL, READINESS_WINDOW_LONG_LABEL, clipWindowState,
               readinessLines, windowWarningsByShot }
        from './src/music_video_producer/web/assets/api.js';
      const report = { window_warnings: [
        { shot_ids: ['both'], labels: ['SHOT 01 (both)'], reason: 'past the band', kind: 'window_long' },
        { shot_ids: ['both'], labels: ['SHOT 01 (both)'], reason: 'off the end of its take', kind: 'take_uncovered' },
      ]};
      // And the same pair in the other order, because a precedence that depended on list order
      // would pass one of these two and fail the other.
      const reversed = { window_warnings: [...report.window_warnings].reverse() };
      console.log(JSON.stringify({
        byShot: windowWarningsByShot(report),
        byShotReversed: windowWarningsByShot(reversed),
        drawn: clipWindowState(windowWarningsByShot(report).both, 'A push-in.'),
        lines: readinessLines(report),
        labels: [READINESS_WINDOW_LONG_LABEL, READINESS_TAKE_UNCOVERED_LABEL],
      }));
    """)
    assert verdicts["byShot"] == {"both": "take_uncovered"}
    assert verdicts["byShotReversed"] == verdicts["byShot"], (
        "the clip's state depends on the order the server listed two notes in, which is not a "
        "decision anyone made"
    )
    assert verdicts["drawn"]["className"] == "take-uncovered"
    # Both lines, each under its own name, and neither under the other's.
    kinds = [line["kind"] for line in verdicts["lines"]]
    assert kinds == ["window-long", "take-uncovered"], kinds
    long_label, uncovered_label = verdicts["labels"]
    assert verdicts["lines"][0]["text"].startswith(long_label)
    assert verdicts["lines"][1]["text"].startswith(uncovered_label)
    assert uncovered_label not in verdicts["lines"][0]["text"]
    # The server's whole sentence, passed through and never reworded here.
    assert verdicts["lines"][1]["reason"] == "off the end of its take"


def test_the_coverage_verdict_the_browser_reads_is_the_one_the_route_answers_with():
    """End to end over a real report, on `test_the_window_verdict_...`'s own argument: a field
    renamed on either side leaves every clip plain for ever with the whole suite green."""
    from music_video_producer.batch import readiness_report

    #: A take recorded for a 5 s window at 10 s: 141 frames of picture (5.875 s), beginning
    #: 0.25 s before the window. The nudge is what a locked move-drag of +1 s writes.
    def take(**overrides):
        fields = {
            "start": 10.0, "duration": 5.0, "prompt": "A corridor push-in.", "mode": "text",
            "latest_output": "shots/x_00001.mp4", "latest_take_lead": 0.25,
            "latest_take_start": 10.0, "latest_take_duration": 5.0, "trim_nudge": 0.0,
        }
        return Shot(**{**fields, **overrides})

    project = Project(
        name="Band",
        shots=[
            take(id="past", trim_nudge=1.0),
            take(id="fine", start=20.0, latest_take_start=20.0, prompt="A steady mid."),
            # No snapshot: every take rendered before 2026-08-21 and every hand-picked clip.
            take(
                id="legacy", start=30.0, latest_take_duration=0.0, trim_nudge=9.0,
                prompt="An older take of the hands.",
            ),
        ],
    )
    report = readiness_report(project)
    payload = json.loads(json.dumps(asdict(report)))
    verdicts = run_module(
        f"const report = {json.dumps(payload)};"
        """
      import { clipWindowState, readinessLines, windowWarningsByShot }
        from './src/music_video_producer/web/assets/api.js';
      const byShot = windowWarningsByShot(report);
      console.log(JSON.stringify({
        byShot,
        past: clipWindowState(byShot.past, 'A corridor push-in.'),
        fine: clipWindowState(byShot.fine, 'A steady mid.'),
        legacy: clipWindowState(byShot.legacy, 'An older take.'),
        lines: readinessLines(report).map((line) => line.text),
      }));
        """
    )
    # Only the shot whose window really left its take, and never the one that cannot be checked.
    assert verdicts["byShot"] == {"past": "take_uncovered"}
    assert verdicts["past"]["className"] == "take-uncovered"
    assert verdicts["fine"] == {"className": "", "note": "", "label": "A steady mid."}
    assert verdicts["legacy"] == {"className": "", "note": "", "label": "An older take."}
    assert len(verdicts["lines"]) == 1, verdicts["lines"]
    assert verdicts["lines"][0].startswith("Past the take - SHOT 01 (past): ")
    assert "does not block submission" in verdicts["lines"][0]
    # The server's own numbers, reaching the Director unreworded.
    assert "5.875s of picture" in verdicts["lines"][0], verdicts["lines"][0]


def test_the_uncovered_clip_is_a_warning_and_not_a_block_anywhere():
    """The band's negative scan, run again for the second yellow: what must not exist is a branch
    in the client that reads this kind and shuts something. The Director was explicit that the
    warning must never constrain -- a guard here would take away the b-roll repositioning the
    trim nudge exists for."""
    for source, name in ((API_JS, "api.js"), (APP_JS, "app.js")):
        for line in source.read_text(encoding="utf-8").splitlines():
            if "take_uncovered" not in line and "TAKE_UNCOVERED" not in line:
                continue
            if line.strip().startswith(("//", "//:", "*")):
                continue
            assert "disabled" not in line, f"{name} shuts a control from the coverage state: {line}"
            assert "refus" not in line.lower(), f"{name} refuses from the coverage state: {line}"
    # And no clamp was added to the move-drag on coverage grounds: the compensation is written
    # from the window's own movement and nothing bounds it.
    clip_drag = APP_JS.read_text(encoding="utf-8").split("function bindClip(clip) {", 1)[1]
    move = clip_drag.split('if (mode === "move") {', 1)[1].split("\n      }", 1)[0]
    assert "clamp(" not in without_comments(move), (
        "the locked move-drag clamps, which stops the gesture instead of colouring it -- the "
        "Director ruled that the warning must never constrain"
    )


# --------------------------------------------------------------------------------------------
# The music lock, beside the trim nudge. The Director's ask, 2026-08-21: "Perhaps a lock/unlock
# from timeline toggle in the shots info panel may be useful next to that nudge input so that
# dragging a b-roll clip would be easier (default locked)."
# --------------------------------------------------------------------------------------------


def test_the_music_lock_defaults_locked_and_is_meaningless_without_a_take():
    """Three states, executed rather than read: locked (the default), unlocked for this one shot,
    and a shot with no take -- where the toggle is not drawn at all, because a live control that
    does nothing is a worse answer than no control."""
    states = run_module("""
      import { TAKE_ANCHOR_CONTROL, TAKE_ANCHOR_HELP, TAKE_ANCHOR_LABEL, takeAnchorControl,
               trimNudgeControl }
        from './src/music_video_producer/web/assets/api.js';
      const rendered = { id: 'a', latest_output: 'shots/a_00001.mp4', latest_take_lead: 0.25 };
      const bare = { id: 'b', latest_output: '' };
      console.log(JSON.stringify({
        locked: takeAnchorControl(rendered),
        lockedExplicit: takeAnchorControl(rendered, false),
        unlocked: takeAnchorControl(rendered, true),
        bare: takeAnchorControl(bare),
        bareUnlocked: takeAnchorControl(bare, true),
        absent: takeAnchorControl(),
        // Drawn on exactly the shots the nudge row is drawn on, because it lives inside it.
        nudgeShown: trimNudgeControl(rendered).shown,
        nudgeBare: trimNudgeControl(bare).shown,
        control: TAKE_ANCHOR_CONTROL, label: TAKE_ANCHOR_LABEL, help: TAKE_ANCHOR_HELP,
      }));
    """)
    # Default locked, which is the Director's own word for it.
    assert states["locked"]["held"] is True
    assert states["lockedExplicit"] == states["locked"]
    assert states["locked"]["shown"] is True
    # Unlocked for this shot: the take travels with the window again, which is what a b-roll
    # reposition wants.
    assert states["unlocked"] == {**states["locked"], "held": False}
    # No take, no lock -- in either direction, and with no arguments at all.
    assert states["bare"]["shown"] is False and states["bare"]["held"] is False
    assert states["bareUnlocked"]["held"] is False
    assert states["absent"]["shown"] is False and states["absent"]["held"] is False
    assert states["nudgeShown"] is True and states["nudgeBare"] is False
    # The label names the state a tick means, and the help names both states and says plainly
    # that this is not the shot lock two rows above it.
    assert states["label"] == "Locked to the music"
    assert "Unlocked" in states["help"] and "b-roll" in states["help"]
    assert "not the shot lock" in states["help"]
    assert "session only" in states["help"]


def test_the_lock_is_session_state_and_never_a_field_on_the_shot():
    """The persistence decision, asserted rather than described. A per-shot persisted flag means a
    new model field, and this repository's recorded guard hole is the generic full-project `PUT`
    writing every defaulted field back -- so a new field earns its keep or it is not added. This
    one would not: what is durable about a b-roll clip is where it sits, and where it sits is
    `start`/`trim_nudge`, which are persisted fields this gesture already writes.

    It also fails closed. A persisted unlock would sit on a lip-sync shot for ever and let a drag
    months later pull its take off the words it was rendered against -- the exact failure the
    Director asked for this to prevent."""
    models = Path("src/music_video_producer/models.py").read_text(encoding="utf-8")
    for spelling in ("take_anchor", "timeline_lock", "unlocked_from_music", "music_lock"):
        assert spelling not in models, f"the music lock became a model field ({spelling})"
    app = APP_JS.read_text(encoding="utf-8")
    # Held as a set of ids in the browser, and never put on a shot object that a save would carry.
    assert "const unlockedFromMusic = new Set();" in app
    assert "shot.take_anchor" not in app and "shot.timeline_lock" not in app
    # Cleared when the project on screen changes, and *only* then: a refresh must not re-tick the
    # box under a Director mid-gesture, and the ids would otherwise collide across projects.
    load = app_js_block("async function loadProject(id) {", "\nasync function")
    assert "unlockedFromMusic.clear();" in load
    cleared = load.split("unlockedFromMusic.clear();", 1)[0]
    assert "documentConsentClearedOnLoad(state.project?.id, id)" in cleared.split("\n")[-3]


def test_the_take_anchoring_rule_is_one_executed_function_with_three_answers():
    """The ruling of 2026-08-21, executed rather than read: "those gestures should only slide the
    window bounds but leave the clip position intact."

    A take's anchor is `start - lead - nudge` -- the song second its first frame plays at -- so a
    window whose `start` moves takes its take with it unless `trim_nudge` follows. `anchoredNudge`
    is the whole rule and the only copy of it; every gesture in `app.js` writes through the one
    door that calls it."""
    answers = run_module("""
      import { anchoredNudge } from './src/music_video_producer/web/assets/api.js';
      const rendered = { id: 'a', latest_output: 'shots/a_00001.mp4', latest_take_lead: 0.25,
                         trim_nudge: 0.125 };
      const bare = { id: 'b', latest_output: '', trim_nudge: 0 };
      const anchor = (shot, nudge) => shot.start - (shot.latest_take_lead || 0) - nudge;
      console.log(JSON.stringify({
        // Locked, with a take: the nudge moves by exactly what the window moved by.
        locked: anchoredNudge(rendered, { from: 10, to: 11.5, nudge: 0.125 }),
        // Backwards is the same rule with the other sign -- the leftward gap fill's direction.
        backwards: anchoredNudge(rendered, { from: 15.017, to: 15.002, nudge: 0.125 }),
        // Unlocked: the Director is repositioning this clip deliberately, so the take travels.
        unlocked: anchoredNudge(rendered, { from: 10, to: 11.5, nudge: 0.125, unlocked: true }),
        // No take: nothing to anchor, and no `trim_nudge` invented for a shot that has none.
        bare: anchoredNudge(bare, { from: 10, to: 11.5, nudge: 0 }),
        bareUnlocked: anchoredNudge(bare, { from: 10, to: 11.5, nudge: 0, unlocked: true }),
        // A window that did not move writes nothing, whatever else is true.
        still: anchoredNudge(rendered, { from: 10, to: 10, nudge: 0.125 }),
        // The nudge defaults to the shot's own field for the gestures that write once.
        defaulted: anchoredNudge(rendered, { from: 10, to: 11.5 }),
        absent: anchoredNudge(undefined, { from: 10, to: 11.5, nudge: 0.5 }),
        // A window whose new position is not a number has not moved anywhere: `NaN` written to a
        // nudge is a shot the whole panel then draws and assembles from as garbage.
        nowhere: anchoredNudge(rendered, { nudge: 0.125 }),
        noArguments: anchoredNudge(rendered),
        // The 17 ms drift, in the numbers a browser measured it in: a 1.608 s move of a window
        // starting at 32.517 s. The anchor has to come out unchanged to the microsecond.
        offGrid: (() => {
          const shot = { ...rendered, start: 32.517, trim_nudge: 0 };
          const to = 34.125;
          const nudge = anchoredNudge(shot, { from: shot.start, to, nudge: 0 });
          return {
            nudge,
            before: anchor(shot, 0),
            after: anchor({ ...shot, start: to }, nudge),
          };
        })(),
        // A hundred pointermoves of one drag, each measured from the drag's own starting pair --
        // which is what stops the compensation compounding once per mouse event.
        dragged: (() => {
          const shot = { ...rendered, start: 32.517, trim_nudge: 0.125 };
          let nudge = 0.125;
          for (let step = 1; step <= 100; step += 1) {
            nudge = anchoredNudge(shot, { from: 32.517, to: 32.517 + step / 100, nudge: 0.125 });
          }
          return nudge;
        })(),
      }));
    """)
    assert answers["locked"] == pytest.approx(1.625)
    assert answers["backwards"] == pytest.approx(0.11)
    assert answers["unlocked"] == pytest.approx(0.125)
    assert answers["bare"] == 0
    assert answers["bareUnlocked"] == 0
    assert answers["still"] == pytest.approx(0.125)
    assert answers["defaulted"] == pytest.approx(1.625)
    assert answers["absent"] == pytest.approx(0.5)
    assert answers["nowhere"] == pytest.approx(0.125)
    assert answers["noArguments"] == pytest.approx(0.125)
    # The anchor is unchanged to the microsecond, which is what `exactSeconds` buys and what the
    # 1/24 s grid cost: `grid(0 + 1.608)` is 1.625, and the take landed 17 ms off the music.
    assert answers["offGrid"]["nudge"] == pytest.approx(1.608)
    assert answers["offGrid"]["after"] == pytest.approx(answers["offGrid"]["before"], abs=1e-9)
    assert answers["dragged"] == pytest.approx(1.125)
    assert "grid(original.nudge" not in APP_JS.read_text(encoding="utf-8"), (
        "a compensation is re-gridded somewhere, which rounds it away from the seconds the "
        "window actually moved and puts the take off the music by up to half a frame"
    )
    # And the rule has one home: nothing re-derives `- lead - nudge` or hand-rolls the sum.
    assert API_JS.read_text(encoding="utf-8").count("takeAnchorControl(shot, unlocked).held") == 1
    for source, name in ((API_JS, "api.js"), (APP_JS, "app.js")):
        assert "original.nudge + (shot.start" not in source.read_text(encoding="utf-8"), (
            f"{name} still spells the compensation out at a call site, which is how this rule "
            "came to be applied at two gestures and not at the other two"
        )


def test_every_write_of_a_shot_start_goes_through_the_one_door():
    """The ruling generalised, asserted structurally -- because the failure it corrects was not a
    wrong rule but a right rule applied at some call sites and not others.

    Every assignment to a `.start` in `app.js` is either a *section* pill's (sections have no
    takes), or inside `moveWindowStart`, or inside `restoreWindow`. A new gesture that writes a
    shot's start anywhere else fails here rather than silently sliding a take off the music."""
    source = APP_JS.read_text(encoding="utf-8")
    door = app_js_block("function moveWindowStart(shot, to, original = null) {", "\n}")
    restore = app_js_block("function restoreWindow(shot, original) {", "\n}")
    writes = [
        line.strip() for line in source.splitlines()
        if re.search(r"\.start\s*=[^=]", line) and not line.strip().startswith("//")
    ]
    assert writes, "the scan found no start writes at all, so it is asserting nothing"
    for line in writes:
        if line.startswith("section.start"):
            continue
        assert line in door or line in restore, (
            f"a shot's start is written outside the anchoring door: {line}"
        )
    # The door asks the rule, and asks it about the shot whose start is moving.
    assert "anchoredNudge(shot, {" in door
    assert "unlocked: unlockedFromMusic.has(shot.id)" in door
    # Never `state.selectedShotId` or any other stand-in for "the clip under the hand": that is
    # precisely the confusion this ruling corrects, and a gesture on A can move B.
    assert "selectedShot" not in door
    # The restore is a restore: it puts the nudge back beside the start it belongs to, and it must
    # never compensate -- compensating a roll-back would move the take by what was rolled back.
    assert "shot.trim_nudge = original.nudge;" in restore
    assert "anchoredNudge" not in restore and "moveWindowStart" not in restore
    # The toggle and every gesture read one function, so the box on screen and the writes can
    # never disagree -- and that function is the contract-tested one, not a re-derivation.
    anchor = without_comments(app_js_block("function takeAnchor(shot) {", "\n}"))
    assert "takeAnchorControl(shot, unlockedFromMusic.has(shot?.id))" in anchor
    # The release still saves a move whose only change was the nudge: `moved()` compares it.
    up = without_comments(app_js_block("const moved = () =>", ";"))
    assert "(shot.trim_nudge || 0) !== original.nudge" in up


def test_the_neighbours_own_lock_governs_the_neighbours_take():
    """The half of the ruling the previous pass declined: "we dont want to slide the take next to
    the one we are adjusting either."

    A right-edge snap moves the *neighbour's* `start` and leaves the dragged shot's alone, so the
    one take that gesture can displace was the one take it never compensated. Both windows now go
    through the door, which reads each target's own id -- so it is B's lock that decides what
    happens to B's take, not the lock of whatever clip the pointer was on."""
    snap = without_comments(app_js_block("function applyPlayheadSnap(", "\n}"))
    # Every window the plan carries, the shared neighbour included, and no special case for the
    # dragged shot: on a right-edge snap its start does not move and the door writes nothing.
    assert "moveWindowStart(target, window.start);" in snap
    assert "target.start =" not in snap
    assert 'mode === "left" && shot.latest_output' not in snap, (
        "the snap still carries its own copy of the compensation, which is the shape that left "
        "the neighbour out"
    )
    # The left edge, which until this ruling decided from `latest_output` alone -- so a rendered
    # shot's left edge dragged the take's buffer out whether the Director had unticked the lock or
    # not. One answer for every gesture: unlocked behaves exactly like a shot with no take, floor
    # and all, because a floor holding a cut off a frame the take is no longer anchored to bounds
    # nothing.
    # Anchored inside `bindClip` -- the section pills have a left branch of their own, and it is
    # not this gesture; sections have no takes.
    clip_drag = APP_JS.read_text(encoding="utf-8").split("function bindClip(clip) {", 1)[1]
    edge = without_comments(
        clip_drag.split('if (mode === "left") {', 1)[1].split("\n        shot.duration =", 1)[0]
    )
    assert "if (takeAnchor(shot).held) {" in edge
    assert "shot.latest_output" not in edge, (
        "the left edge still decides from the take's existence rather than from the lock"
    )
    assert edge.count("moveWindowStart(shot,") == 2, edge
    # The gap fill's one direction that moves a start: leftward, this shot's own.
    fill = without_comments(app_js_block("function runGapFill(shotId, edge) {", "\n}"))
    assert "moveWindowStart(shot, plan.start);" in fill
    assert "shot.start =" not in fill
    # And the planner it reads still never moves the neighbour, in either direction -- which is
    # what makes "this shot's own start, leftward only" the complete account of the gesture.
    windows = run_module("""
      import { gapFillPlan } from './src/music_video_producer/web/assets/api.js';
      const project = { song: { duration: 60 }, shots: [
        { id: 'a', start: 0, duration: 5, prompt: 'x', mode: 'text' },
        { id: 'b', start: 15.017, duration: 5, prompt: 'x', mode: 'text' },
        { id: 'c', start: 25, duration: 5, prompt: 'x', mode: 'text' },
      ] };
      console.log(JSON.stringify({
        left: gapFillPlan(project, 'b', 'left'),
        right: gapFillPlan(project, 'b', 'right'),
      }));
    """)
    # Leftward: this shot's start moves back to meet the clip behind it.
    assert windows["left"]["start"] == pytest.approx(5.0)
    assert windows["left"]["duration"] == pytest.approx(15.017)
    # Rightward: the start stays exactly where it was and only the duration grows, so there is no
    # take anywhere that this direction could move.
    assert windows["right"]["start"] == pytest.approx(15.017)
    assert windows["right"]["duration"] == pytest.approx(9.983)


def test_undo_restores_a_snapshot_and_never_compensates_on_top_of_it():
    """The one place the rule must *not* reach. Undo and redo replay a snapshotted shot list
    through `PUT /shots`, and that snapshot already carries the `start` and the `trim_nudge` that
    belonged together at the moment it was taken. Compensating a restore would move every anchored
    take by the amount the gesture had already been rolled back by -- the rule applied twice."""
    step = without_comments(app_js_block("async function stepHistory(from, onto, redo) {", "\n}"))
    assert "moveWindowStart" not in step and "anchoredNudge" not in step
    assert "trim_nudge" not in step
    # The whole list is adopted from the server's reply, field for field -- there is no per-shot
    # write here for a compensation to attach itself to.
    assert "state.project.shots = saved.shots;" in step
    assert "api.saveShots(projectId, entry.shots, undoRevision)" in step
    # And the snapshot it replays is a structural clone of the plan, not a re-derivation of it.
    save = without_comments(app_js_block('function saveShotsSilently(kind = "edit") {', "\n}"))
    assert "structuredClone(shotsBaseline)" in save
    assert "moveWindowStart" not in save and "anchoredNudge" not in save


def test_the_lock_is_drawn_next_to_the_nudge_and_writes_nothing_when_toggled():
    """"next to that nudge input" -- so it is inside the trim-nudge row, which also makes it
    appear on exactly the shots the nudge appears on. Toggling saves nothing: it changes what the
    *next* drag does, and the plan on disk is untouched until that drag happens."""
    inspector = APP_JS.read_text(encoding="utf-8").split("export function renderShotInspector()", 1)[1]
    inspector = inspector.split("\nfunction updateShotFromInspector", 1)[0]
    assert "const anchor = takeAnchor(shot);" in inspector
    assert 'const anchorHtml = anchor.shown' in inspector
    # Inside the nudge row, after Reset and before the offset readout.
    nudge_row = inspector.split('<div class="trim-nudge" id="trim-nudge">', 1)[1].split("</div>", 1)[0]
    assert "${anchorHtml}" in nudge_row
    assert nudge_row.index("nudge-reset") < nudge_row.index("${anchorHtml}")
    assert nudge_row.index("${anchorHtml}") < nudge_row.index("control-reason")
    # Bound to the id the control decided, never to a literal typed twice.
    handler = without_comments(
        inspector.split('$("#" + anchor.control)?.addEventListener("change"', 1)[1]
        .split("});", 1)[0]
    )
    assert "unlockedFromMusic.delete(shot.id)" in handler
    assert "unlockedFromMusic.add(shot.id)" in handler
    assert "saveShotsSilently" not in handler, "toggling the lock wrote the plan to the server"
    assert "renderTimeline" not in handler, (
        "toggling the lock rebuilds the panel under the Director's own click, which is this "
        "application's recorded way of losing the control they just pressed"
    )


# --------------------------------------------------------------------------------------------
# Fill section looks: the browser half. The Director's report (2026-08-20) was made *at* the
# section inspector — "I clicked on a Section in the timeline and noticed that the shared prompt
# wasnt pre-filled with information from the Treatment" — so the control lives there, and the
# confirm text is executed under node rather than read out of `app.js`.
# --------------------------------------------------------------------------------------------

#: A report in the shape `SectionLooksResponse` really answers with, including the two skips
#: that carry the server's own sentences.
SECTION_LOOKS_REPORT = {
    "applied": False,
    "filled": 2,
    "skipped": 2,
    "message": "2 filled, 2 left alone",
    "project": None,
    "sections": [
        {"section_id": "section_a", "label": "Intro", "start": 0.0, "filled": True,
         "prompt": "The empty moonlit warehouse.", "previous": "", "reason": ""},
        {"section_id": "section_b", "label": "Verse", "start": 11.0, "filled": True,
         "prompt": "Handheld at the chrome mic.", "previous": "", "reason": ""},
        {"section_id": "section_c", "label": "Bridge", "start": 103.2, "filled": False,
         "prompt": "", "previous": "",
         "reason": "the treatment does not describe this section"},
        {"section_id": "section_d", "label": "Outro", "start": 124.1, "filled": False,
         "prompt": "A proposed outro look.", "previous": "My own outro look.",
         "reason": SECTION_LOOK_SKIP_WRITTEN},
    ],
}


def test_the_section_look_confirmation_names_every_section_and_every_skip_reason():
    """The report is the half that makes this safe, so nothing in it is summarised away.

    A skipped section's line is the **server's own sentence**, unedited — `snapCutsReportLines`'
    rule and for its reason. "The treatment does not describe this section" is what sends the
    Director back to the treatment; swallowed, the box is just mysteriously still blank.
    """
    text = run_module("""
      import { sectionLooksConfirmation } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        filled: sectionLooksConfirmation(__REPORT__),
        empty: sectionLooksConfirmation(null),
      }));
    """.replace("__REPORT__", json.dumps(SECTION_LOOKS_REPORT)))

    assert text["empty"] == ""
    confirmation = text["filled"]
    assert confirmation.startswith("2 filled, 2 left alone")
    for row in SECTION_LOOKS_REPORT["sections"]:
        assert row["label"] in confirmation
        assert (row["prompt"] if row["filled"] else row["reason"]) in confirmation
    # The proposal for a section that will NOT be written is shown as a skip, never as a line
    # that reads like it is about to land.
    assert "124.1s Outro: skipped — already has a look you wrote" in confirmation
    assert confirmation.rstrip().endswith("Write these looks?")


#: The report a fully written structure gets back, and the exact shape of the Director's live
#: project: the route short-circuits it **without a model call**, so `filled` is 0, every row
#: carries the look they wrote, and the message names the consent word.
SECTION_LOOKS_ALL_WRITTEN_REPORT = {
    "applied": False,
    "filled": 0,
    "skipped": 2,
    "message": SECTION_LOOKS_ALL_WRITTEN,
    "sections": [
        {"section_id": "section_intro", "label": "Intro", "start": 0.0, "filled": False,
         "prompt": "", "previous": "Mine: the corridor, low and slow.",
         "reason": SECTION_LOOK_SKIP_ALL_WRITTEN},
        {"section_id": "section_outro", "label": "Outro", "start": 11.0, "filled": False,
         "prompt": "", "previous": "Mine: the door, closing on the light.",
         "reason": SECTION_LOOK_SKIP_ALL_WRITTEN},
    ],
}


def test_the_overwrite_consent_is_reachable_when_every_section_is_already_written():
    """Review Finding 3, 2026-08-21. The handler bailed with
    `if (!report.filled) return toast(report.message, "error")` **before** it ever asked the
    overwrite question — and a structure where every section already carries a look is exactly
    the state the route answers with `0 filled`. So for the Director's live project the button
    could only ever error, while the sentence it showed described a consent the screen had no way
    to give.

    Driven end to end against a real server and a real browser in `tests/e2e_section_looks.py`,
    where the looks are read back off the stored manifest; this is the fast gate on the same rule.
    """
    driven = run_workspace(
        """
        state.project = {
          id: 'p9', updated_at: 'rev-1', name: 'x', assets: [], jobs: [], messages: [],
          shots: [], song: { duration: 140, path: 'songs/000-x.wav' },
          sections: [
            { id: 'section_intro', label: 'Intro', start: 0, duration: 11,
              prompt: 'Mine: the corridor, low and slow.' },
            { id: 'section_outro', label: 'Outro', start: 11, duration: 11,
              prompt: 'Mine: the door, closing on the light.' },
          ],
        };
        state.selectedSectionId = 'section_intro';
        app.renderShotInspector();
        answer(false);
        await fire('#section-fill-looks:click', {});
        await flush();
        console.log(JSON.stringify({
          question: contract.FILL_SECTION_LOOKS_OVERWRITE_QUESTION,
          asked,
          sent: requests.filter((entry) => entry.method === 'POST')
            .map((entry) => JSON.parse(entry.body)),
          panel: String(at('#shot-inspector').innerHTML),
        }));
        """,
        responses={
            "/api/projects/p9/sections/fill-looks": {
                "body": SECTION_LOOKS_ALL_WRITTEN_REPORT
            },
        },
    )
    assert driven["asked"] == [driven["question"]], (
        "the overwrite consent was not the question a fully written structure asks", driven
    )
    # Declined, so nothing further goes on the wire: one report, no confirm, no apply.
    assert driven["sent"] == [{"confirm_apply": False, "overwrite": False, "plan": None}], (
        driven["sent"]
    )
    # And the reasons survive the dialog rather than vanishing with it: the panel names every
    # section, why it was skipped, and the words the consent would have replaced.
    for row in SECTION_LOOKS_ALL_WRITTEN_REPORT["sections"]:
        assert row["label"] in driven["panel"], (row["label"], driven["panel"])
        assert row["reason"] in driven["panel"], driven["panel"]
        assert row["previous"] in driven["panel"], (
            ("the look the consent would replace is not on screen beside the question "
             "about replacing it"), driven["panel"],
        )


def test_the_section_look_report_says_what_a_look_would_replace():
    """A look the Director wrote themselves is the one thing this pass can destroy, so the
    overwrite consent is only a real question while the words it takes away are on screen beside
    the words it puts there. "6 filled, 1 left alone" is not a sentence anybody can agree to."""
    lines = run_module("""
      import { sectionLooksReportLines }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        replacing: sectionLooksReportLines({ sections: [
          { label: 'Chorus', start: 35, filled: true, prompt: 'The canopy bed, wide.',
            previous: 'Mine: the chrome mic.', reason: '' },
        ] }),
        fresh: sectionLooksReportLines({ sections: [
          { label: 'Intro', start: 0, filled: true, prompt: 'The corridor.',
            previous: '', reason: '' },
        ] }),
        withheld: sectionLooksReportLines({ sections: [
          { label: 'Outro', start: 124, filled: false, prompt: 'The door, closing.',
            previous: 'Mine: the door.',
            reason: __SKIP_WRITTEN__ },
        ] }),
        silent: sectionLooksReportLines({ sections: [
          { label: 'Bridge', start: 103, filled: false, prompt: '', previous: '',
            reason: 'the treatment does not describe this section' },
        ] }),
        nothing: sectionLooksReportLines(null),
      }));
    """.replace("__SKIP_WRITTEN__", json.dumps(SECTION_LOOK_SKIP_WRITTEN)))
    assert lines["nothing"] == []
    # A look that lands over one the Director wrote names both.
    replacing = lines["replacing"][0]
    assert replacing["kind"] == "fill"
    assert "The canopy bed, wide." in replacing["text"]
    assert "Mine: the chrome mic." in replacing["text"], (
        "a look about to replace hand-written words did not say which words", replacing
    )
    # A look landing in an empty box replaces nothing, and does not claim to.
    assert lines["fresh"][0]["text"] == "0.0s Intro: The corridor."
    # A skip carries what saying yes would buy, *and* what is there now -- the route puts the
    # proposal on that row for exactly this reason.
    withheld = lines["withheld"][0]
    assert withheld["kind"] == "skip"
    assert "already has a look you wrote" in withheld["text"]
    assert "The door, closing." in withheld["text"], withheld
    assert "Mine: the door." in withheld["text"], withheld
    # And a skip with nothing behind it stays one sentence.
    assert lines["silent"][0]["text"] == (
        "103.0s Bridge: skipped — the treatment does not describe this section"
    )


def test_the_fill_section_looks_control_sits_in_the_section_inspector_and_reports_first():
    """Where the gap was reported, and in the order the server enforces.

    The control is drawn in the section inspector beside the shared prompt it fills, the first
    call carries no confirmation (a report), and the overwrite consent is a **second** question
    — asked only when the report says a hand-written look is in the way.
    """
    app = APP_JS.read_text(encoding="utf-8")
    api = API_JS.read_text(encoding="utf-8")
    # Drawn in the same template string as the shared prompt textarea, which is the inspector.
    inspector = next(line for line in app.splitlines() if 'id="section-prompt"' in line)
    assert 'id="section-fill-looks"' in inspector
    # Report first: the un-flagged call, then a confirm, then the applying call.
    handler = app.split('#section-fill-looks")?.addEventListener')[1].split(
        '$("#section-delete")'
    )[0]
    assert handler.index("api.fillSectionLooks(projectId)") < handler.index("window.confirm(")
    assert handler.index("window.confirm(") < handler.index("confirmApply: true")
    assert "FILL_SECTION_LOOKS_OVERWRITE_QUESTION" in handler
    # Both flags are on the wire, and the route is the server's.
    assert "/sections/fill-looks" in api
    assert "confirm_apply: confirmApply, overwrite" in api


def test_a_second_press_on_an_edge_is_only_the_same_gesture_while_the_window_is_open():
    """Mutation testing found this branch unheld: dropping the elapsed-time check left every
    press on an edge that had *ever* been pressed reading as a double-click, so a shot resized
    at the start of a session would have its gap closed by the next touch on the same handle."""
    presses = run_module("""
      import { EDGE_DOUBLE_CLICK_MS, doubleEdgePress }
        from './src/music_video_producer/web/assets/api.js';
      const first = { shotId: 's1', edge: 'right', at: 1_000_000 };
      const at = (delta, extra = {}) => doubleEdgePress(first, { ...first, at: first.at + delta, ...extra });
      console.log(JSON.stringify({
        window: EDGE_DOUBLE_CLICK_MS,
        immediate: at(0),
        inside: at(EDGE_DOUBLE_CLICK_MS - 1),
        exactly: at(EDGE_DOUBLE_CLICK_MS),
        justOutside: at(EDGE_DOUBLE_CLICK_MS + 1),
        muchLater: at(120_000),
        otherEdge: at(50, { edge: 'left' }),
        otherShot: at(50, { shotId: 's2' }),
        backwards: at(-50),
        nothingBefore: doubleEdgePress(null, first),
        notAnEdge: doubleEdgePress(first, { shotId: 's1', edge: '', at: first.at + 50 }),
      }));
    """)
    assert presses["window"] == 400
    assert presses["immediate"] is True
    assert presses["inside"] is True
    assert presses["exactly"] is True
    assert presses["justOutside"] is False
    assert presses["muchLater"] is False, (
        "a press two minutes after the last one closed a gap nobody asked to close"
    )
    # A different edge, or a different shot, is a different gesture.
    assert presses["otherEdge"] is False
    assert presses["otherShot"] is False
    # And nothing pathological counts: a clock that went backwards, no previous press, or a
    # press on the body of a clip rather than on one of its handles.
    assert presses["backwards"] is False
    assert presses["nothingBefore"] is False
    assert presses["notAnEdge"] is False


def test_a_drag_consumes_the_press_that_started_it():
    """Review Finding 5, 2026-08-21. A press on a resize handle starts *both* gestures -- the
    drag and the first half of a double-click -- and only the release can tell them apart.
    `lastEdgePress` was cleared only when a double-press completed, and `doubleEdgePress`
    measures from the *first* press, so a 300 ms edge drag followed by re-grabbing the same edge
    100 ms later fell inside the window and ran the gap fill instead of starting the second drag.

    The slop is what keeps the fix from taking gesture B away: a real double-click on a 7 px
    handle does not travel, and a hand that jitters a pixel between clicks is still
    double-clicking. The browser drives both directions in `tests/e2e_timeline_edit.py`.
    """
    travel = run_module("""
      import { EDGE_DRAG_SLOP_PX, edgePressSurvivesDrag }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        slop: EDGE_DRAG_SLOP_PX,
        still: edgePressSurvivesDrag(0),
        jitter: edgePressSurvivesDrag(EDGE_DRAG_SLOP_PX),
        justOver: edgePressSurvivesDrag(EDGE_DRAG_SLOP_PX + 1),
        drag: edgePressSurvivesDrag(60),
        backwards: edgePressSurvivesDrag(-60),
        nothingMeasured: edgePressSurvivesDrag(undefined),
      }));
    """)
    assert travel["slop"] == 3
    # A click, and a click with a shaky hand, are both still clicks.
    assert travel["still"] is True
    assert travel["jitter"] is True
    assert travel["nothingMeasured"] is True
    # A drag is a drag in either direction, and it takes its press with it.
    assert travel["justOver"] is False
    assert travel["drag"] is False
    assert travel["backwards"] is False, (
        "a leftward drag left its press standing, so the next grab on that edge would close a "
        "gap nobody asked to close"
    )


def test_one_undo_steps_back_one_gesture_when_two_were_made_in_one_round_trip():
    """Review Finding 4, 2026-08-21, and the invariant this whole section is built on: the state
    before save N is the state after save N-1.

    `restores` used to be cloned when a save was *queued*, from a baseline that only advances
    when a save *lands*. Two gestures made before the first write came back therefore recorded
    the same "before": one Undo rolled back **both** while the button named only the second, a
    second Undo replayed the same plan and did nothing visible, and the plan between the two
    gestures could not be reached at all.

    Driven here by holding the first write open, which is the only way the window exists -- and
    driven again as two real clicks against a real server in `tests/e2e_timeline_edit.py`, where
    the plan is read back off disk.
    """
    driven = run_workspace(
        """
        state.project = {
          id: 'p9', updated_at: 'rev-1', name: 'x', assets: [], jobs: [], messages: [],
          sections: [], song: { duration: 20, path: 'songs/000-x.wav' },
          shots: [
            { id: 's1', start: 0, duration: 10, prompt: 'one', status: 'draft', citations: [] },
            { id: 's2', start: 10, duration: 10, prompt: 'two', status: 'draft', citations: [] },
          ],
        };
        state.selectedShotId = 's1';
        app.syncUndoControls();
        // Both gestures are made before either write comes back. `setTimeout` is stubbed out in
        // this harness, so the writes are held by hand instead of by a timer.
        const release = [];
        const real = globalThis.fetch;
        let revision = 1;
        globalThis.fetch = (path, options = {}) => {
          if (options.method !== 'PUT') return real(path, options);
          requests.push({ path, method: 'PUT', body: options.body });
          return new Promise((resolve) => release.push(() => {
            revision += 1;
            resolve({
              ok: true, status: 200, statusText: 'held',
              headers: { get: () => 'application/json' },
              json: async () => ({ id: 'p9', updated_at: 'rev-' + revision, shots: [] }),
            });
          }));
        };
        fire('#split-shot:click', {});
        fire('#add-shot:click', {});
        await flush();
        const openWrites = release.length;
        release[0]();
        await flush();
        release[1]();
        await flush();
        const afterBoth = state.project.shots.length;
        const named = at('#undo-shots').title;
        requests.length = 0;
        fire('#undo-shots:click', {});
        await flush();
        release[2]();
        await flush();
        const undone = requests.filter((entry) => entry.method === 'PUT');
        console.log(JSON.stringify({
          openWrites,
          afterBoth,
          named,
          restored: undone.length ? JSON.parse(undone[0].body).shots.length : null,
          namedAfter: at('#undo-shots').title,
        }));
        """,
    )
    # Only the first write was ever in flight: the chain sends them one at a time, and the
    # second gesture was made while the first was open. That is the whole window.
    assert driven["openWrites"] == 1, driven
    assert driven["afterBoth"] == 4, driven
    assert "adding a shot" in driven["named"], driven
    assert driven["restored"] == 3, (
        "one Undo did not step back exactly one gesture: it put back a plan of "
        f"{driven['restored']} shots, and the plan between the two gestures had 3 -- Finding 4"
    )
    assert "the split" in driven["namedAfter"], (
        "after stepping back the second gesture the button does not name the first", driven
    )


def test_a_gesture_saved_after_another_writer_moved_the_project_records_nothing():
    """Mutation testing found this branch unheld too, and it is the sharpest edge in the whole
    design: `restores` is the plan as the *last landed save* left it, so if some other route
    wrote in between, it is the plan before **that writer** and not before this click. Undoing
    to it would silently revert their work. The entry is dropped rather than kept."""
    driven = run_workspace(
        """
        state.project = {
          id: 'p9', updated_at: 'rev-1', name: 'x', assets: [], jobs: [], messages: [],
          sections: [], song: { duration: 20, path: 'songs/000-x.wav' },
          shots: [
            { id: 's1', start: 0, duration: 10, prompt: 'one', status: 'draft', citations: [] },
            { id: 's2', start: 10, duration: 10, prompt: 'two', status: 'draft', citations: [] },
          ],
        };
        state.selectedShotId = 's1';
        app.syncUndoControls();
        fire('#split-shot:click', {});
        await flush();
        const afterSplit = { disabled: at('#undo-shots').disabled, name: at('#undo-shots').title };
        // Somebody else's route answered with the whole project and this client adopted it --
        // an approve, a mark-ready, a take swap. The revision moves; the shot list this stack
        // describes does not exist any more.
        state.project.updated_at = 'rev-from-another-writer';
        fire('#add-shot:click', {});
        await flush();
        console.log(JSON.stringify({
          afterSplit,
          afterForeign: { disabled: at('#undo-shots').disabled, name: at('#undo-shots').title },
          writes: requests.filter((entry) => entry.method === 'PUT').length,
        }));
        """,
        responses={
            "/api/projects/p9/shots": {
                "body": {
                    "id": "p9",
                    "updated_at": "rev-2",
                    "shots": [
                        {"id": "s1", "start": 0, "duration": 10, "status": "draft"},
                        {"id": "s2", "start": 10, "duration": 10, "status": "draft"},
                    ],
                }
            },
        },
    )
    # The split alone is undoable, and says so.
    assert driven["afterSplit"]["disabled"] is False
    assert "the split" in driven["afterSplit"]["name"]
    # Both writes went out -- nothing here refuses an edit, only the *history* is dropped.
    assert driven["writes"] == 2, driven
    assert driven["afterForeign"]["disabled"] is True, (
        "an undo is offered after another writer moved the project, and the snapshot it holds "
        "is the plan from before that writer -- pressing it would revert their work"
    )
    assert "Nothing to undo" in driven["afterForeign"]["name"], driven["afterForeign"]


# ------------------------------------------------------------------------------------------------
# The seed's randomize toggle, and the Assets panel's subtabs (the Director's asks, 2026-08-20).
# ------------------------------------------------------------------------------------------------


def test_a_queued_retake_moves_its_seed_by_exactly_one_rule():
    """`nextRenderSeed` is the only place a retake's seed moves, so the two sources cannot fight.

    There are two of them now: the server's own RESUBMIT_SEED_STRIDE, which the lone-click
    render-again has applied since 2026-08-19, and the Director's randomize toggle. Applying both
    would put an invisible drift on the one value the Director has just asked to own; applying
    neither would resubmit at the same seed and prompt, which reproduces the identical take and
    reads as "nothing was replaced".

    Driven with an injected `random`, so the edges are exercised rather than sampled.
    """
    from music_video_producer.app import RESUBMIT_SEED_STRIDE

    moved = run_module("""
      import { RANDOM_SEED_MAX, RANDOM_SEED_MIN, RESUBMIT_SEED_STRIDE, nextRenderSeed, randomSeed }
        from './src/music_video_producer/web/assets/api.js';
      const at = (value) => () => value;
      console.log(JSON.stringify({
        min: RANDOM_SEED_MIN,
        max: RANDOM_SEED_MAX,
        stride: RESUBMIT_SEED_STRIDE,
        // Randomize off: the stride, exactly as before this toggle existed.
        fixed: nextRenderSeed({ seed: 7 }, false, at(0.5)),
        fixedFromZero: nextRenderSeed({ seed: 0 }, false, at(0.5)),
        fixedMissing: nextRenderSeed({}, false, at(0.5)),
        // Randomize on: a roll INSTEAD of the stride, never as well as it.
        rolledLow: nextRenderSeed({ seed: 7 }, true, at(0)),
        rolledHigh: nextRenderSeed({ seed: 7 }, true, at(0.9999999999)),
        rolledMid: nextRenderSeed({ seed: 7 }, true, at(0.5)),
        // A roll that lands on the seed already stored is nudged on: the gesture's whole point
        // is a DIFFERENT take.
        collision: nextRenderSeed({ seed: 50000 }, true, at(0.5)),
        collisionAtCeiling: nextRenderSeed({ seed: RANDOM_SEED_MAX }, true, at(0.9999999999)),
        // The roll itself, at both ends and past them.
        rollFloor: randomSeed(at(0)),
        rollCeiling: randomSeed(at(0.9999999999)),
        rollOverflow: randomSeed(at(1)),
      }));
    """)

    assert moved["stride"] == RESUBMIT_SEED_STRIDE
    assert moved["min"] == 1 and moved["max"] == 99999

    # Randomize off is byte-for-byte the behaviour that shipped: the server's own stride.
    assert moved["fixed"] == 7 + RESUBMIT_SEED_STRIDE
    assert moved["fixedFromZero"] == RESUBMIT_SEED_STRIDE
    assert moved["fixedMissing"] == RESUBMIT_SEED_STRIDE

    # Randomize on replaces the stride rather than adding to it -- every one of these is inside
    # the Director's 1-99999, and none of them is `seed + 101`.
    for case in ("rolledLow", "rolledHigh", "rolledMid", "collision", "collisionAtCeiling"):
        assert 1 <= moved[case] <= 99999, (case, moved[case])
        assert moved[case] != 7 + RESUBMIT_SEED_STRIDE, case
    assert moved["rolledLow"] == 1
    assert moved["rolledHigh"] == 99999
    assert moved["rolledMid"] == 50000

    # ...and never the number already stored.
    assert moved["collision"] == 50001
    assert moved["collisionAtCeiling"] == 1

    # The roll is clamped into the bounds at both ends, including the `Math.random() === 1` that
    # the specification forbids and no engine promises never to hand back.
    assert moved["rollFloor"] == 1
    assert moved["rollCeiling"] == 99999
    assert moved["rollOverflow"] == 99999


def test_randomize_rolls_once_holds_across_redraws_and_is_cleared_by_a_hand_typed_seed():
    """The toggle's whole behaviour, executed: roll on tick, hold, re-roll only on Render again.

    The Director asked for a number that "would RNG a number and hold it unless regenerate gets hit
    later with randomize still checked", so the three claims worth executing are that ticking
    writes a seed, that redrawing the panel does not write another one, and that typing a number by
    hand takes the toggle back off -- typing a specific seed is a statement that you want that seed.
    """
    driven = run_workspace(r"""
      // A fixed roll, so the assertions are about the rule rather than about luck.
      Math.random = () => 0.5;
      const project = () => ({
        id: 'p1', assets: [], jobs: [], song: null, messages: [],
        shots: [{ id: 'shot_a', start: 0, duration: 5, prompt: 'A singer turns toward camera',
                  mode: 'text', asset_ids: [], citations: [], reference_labels: {},
                  use_song_audio: false, seed: 12, status: 'complete', prompt_id: 'p-1',
                  latest_output: '', approved_output: '', locked: false }],
      });
      state.project = project();
      state.selectedShotId = 'shot_a';
      app.renderShotInspector();
      const before = at('#shot-inspector').innerHTML;

      // Tick it. One roll, written through the ordinary silent shot save.
      requests.length = 0;
      fire('#shot-seed-randomize:change', { target: { checked: true } });
      await flush();
      const afterTick = {
        seed: state.project.shots[0].seed,
        html: at('#shot-inspector').innerHTML,
        writes: requests.filter((item) => item.method === 'PUT').length,
      };

      // Redraw the panel the way every unawaited reply in this application does. It must not roll
      // again: "hold it" is the Director's own word.
      requests.length = 0;
      app.renderShotInspector();
      app.renderShotInspector();
      const afterRedraw = {
        seed: state.project.shots[0].seed,
        html: at('#shot-inspector').innerHTML,
        writes: requests.filter((item) => item.method === 'PUT').length,
      };

      // Type a seed by hand. That clears the toggle, in the same gesture.
      at('#shot-seed').value = '4242';
      fire('#shot-seed:change', {});
      await flush();
      const afterTyping = {
        seed: state.project.shots[0].seed,
        html: at('#shot-inspector').innerHTML,
      };

      // Unticking writes nothing and keeps the number: it becomes an ordinary fixed seed.
      fire('#shot-seed-randomize:change', { target: { checked: true } });
      await flush();
      const rolledAgain = state.project.shots[0].seed;
      requests.length = 0;
      fire('#shot-seed-randomize:change', { target: { checked: false } });
      await flush();
      // Read after a redraw, deliberately: unticking re-renders nothing (the browser has already
      // cleared the box the Director clicked), so the markup still on screen is the markup from
      // before the click. What has to be true is that the NEXT rebuild draws it off.
      const untickedWrites = requests.filter((item) => item.method === 'PUT').length;
      app.renderShotInspector();
      const afterUntick = {
        seed: state.project.shots[0].seed,
        writes: untickedWrites,
        html: at('#shot-inspector').innerHTML,
      };

      const ticked = (html) => /id="shot-seed-randomize"[^>]*\schecked/.test(html);
      console.log(JSON.stringify({
        beforeTicked: ticked(before),
        afterTick: { ...afterTick, ticked: ticked(afterTick.html) },
        afterRedraw: { ...afterRedraw, ticked: ticked(afterRedraw.html) },
        afterTyping: { ...afterTyping, ticked: ticked(afterTyping.html) },
        rolledAgain,
        afterUntick: { ...afterUntick, ticked: ticked(afterUntick.html) },
        shortBox: /class="seed-field"/.test(afterTick.html),
        label: afterTick.html.includes('Randomize on Render again'),
      }));
    """)

    # It ships off. Nothing rolls a seed until the Director asks for one.
    assert driven["beforeTicked"] is False

    # Ticking rolls once, inside the Director's bounds, and saves it -- so the number on screen is
    # the number a render would use, rather than a promise about a future one.
    assert driven["afterTick"]["seed"] == 50000
    assert driven["afterTick"]["ticked"] is True
    assert driven["afterTick"]["writes"] == 1

    # It holds. Two redraws, no roll, no write.
    assert driven["afterRedraw"]["seed"] == 50000
    assert driven["afterRedraw"]["ticked"] is True
    assert driven["afterRedraw"]["writes"] == 0

    # Typing a number by hand takes the toggle off, and the typed number stands.
    assert driven["afterTyping"]["seed"] == 4242
    assert driven["afterTyping"]["ticked"] is False, (
        "a hand-typed seed left the randomizer armed, so the next Render again would throw the "
        "Director's own number away"
    )

    # Unticking is not an undo: the rolled number stays, as an ordinary fixed seed, and nothing
    # is written.
    assert driven["rolledAgain"] == 50000
    assert driven["afterUntick"]["seed"] == 50000
    assert driven["afterUntick"]["ticked"] is False
    assert driven["afterUntick"]["writes"] == 0

    # The box is the shortened one, and the label names the moment it re-rolls rather than
    # leaving it to be guessed.
    assert driven["shortBox"] is True
    assert driven["label"] is True


def test_the_randomize_help_names_the_re_roll_moment_and_the_two_gestures_that_are_not_it():
    """A toggle whose re-roll moment has to be guessed is worse than a button.

    So the help text has to name the gesture that re-rolls in the inspector's own word for it, and
    the two nearby gestures that do not -- Mark ready queues nothing, and Generate All strides on
    the server without reading this box at all.
    """
    help_text = run_module("""
      import { RANDOM_SEED_HELP, RANDOM_SEED_LABEL, RANDOM_SEED_CONTROL, RENDER_AGAIN_LABEL,
        MARK_READY_LABEL } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        help: RANDOM_SEED_HELP, label: RANDOM_SEED_LABEL, control: RANDOM_SEED_CONTROL,
        renderAgain: RENDER_AGAIN_LABEL, markReady: MARK_READY_LABEL,
      }));
    """)

    assert help_text["control"] == "shot-seed-randomize"
    # The label alone answers "when does this re-roll", without a hover.
    assert help_text["renderAgain"] in help_text["label"], help_text["label"]
    assert "1-99999" in help_text["help"].replace("–", "-")
    assert help_text["renderAgain"] in help_text["help"]
    assert help_text["markReady"].split(" to ")[0] in help_text["help"]
    assert "Generate All" in help_text["help"]
    # And it says which of the two answers to "does this stick" is true.
    assert "not saved into the project" in help_text["help"]


def test_the_asset_subtabs_cover_every_asset_kind_the_model_allows():
    """An asset under no tab is one a Director can neither cite, replace nor delete from here.

    `models.AssetKind` has seven members and the Director named four subtabs, so this is the guard
    that the three they did not name -- `image`, `audio`, `video` -- landed somewhere rather than
    being dropped, and that a kind added later cannot go invisible by omission.
    """
    tabs = run_module("""
      import { ASSET_TABS, ASSET_TAB_DEFAULT, assetTab }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        tabs: ASSET_TABS.map((tab) => ({ id: tab.id, label: tab.label, kinds: tab.kinds })),
        fallback: assetTab('a-tab-that-was-removed').id,
        byDefault: assetTab(undefined).id,
        declaredDefault: ASSET_TAB_DEFAULT,
      }));
    """)

    ids = [tab["id"] for tab in tabs["tabs"]]
    # The Director's four, the clips as their own subtab, and All kept because it is the strip's
    # existing behaviour.
    assert ids == ["all", "character", "setting", "prop", "style", "media", "clips"], ids

    covered = set()
    for tab in tabs["tabs"]:
        if tab["id"] == "all":
            assert tab["kinds"] is None, "the All tab must not be a hand-maintained kind list"
            continue
        covered.update(tab["kinds"] or [])
    assert covered == set(get_args(AssetKind)), (
        "these asset kinds appear under no subtab, so an asset of that kind is invisible and "
        f"undeletable from the Assets panel: {sorted(set(get_args(AssetKind)) - covered)}"
    )
    # No kind is claimed by two tabs, or the same asset is drawn under two headings.
    listed = [kind for tab in tabs["tabs"] if tab["kinds"] for kind in tab["kinds"]]
    assert len(listed) == len(set(listed)), listed
    # The clips tab is not an asset view at all, and says so by carrying no kinds.
    assert dict(zip(ids, [tab["kinds"] for tab in tabs["tabs"]]))["clips"] == []

    # An unknown stored tab lands on a real one rather than emptying the panel.
    assert tabs["fallback"] == tabs["declaredDefault"] == "all"
    assert tabs["byDefault"] == "all"


def test_each_asset_subtab_filters_its_own_kinds_and_says_so_when_it_is_empty():
    """Decided once, so the grid and the message under it cannot disagree about the count."""
    sorted_out = run_module("""
      import { ASSET_TABS, assetsForTab, assetTabEmpty }
        from './src/music_video_producer/web/assets/api.js';
      const assets = [
        { id: 'a1', kind: 'character', name: 'Lucy' },
        { id: 'a2', kind: 'setting', name: 'Corridor' },
        { id: 'a3', kind: 'prop', name: 'Red guitar' },
        { id: 'a4', kind: 'style', name: 'Grain plate' },
        { id: 'a5', kind: 'image', name: 'Reference still' },
        { id: 'a6', kind: 'audio', name: 'Room tone' },
        { id: 'a7', kind: 'video', name: 'Stock plate' },
      ];
      const per = {};
      for (const tab of ASSET_TABS) per[tab.id] = assetsForTab(assets, tab.id).map((a) => a.id);
      console.log(JSON.stringify({
        per,
        searched: assetsForTab(assets, 'all', 'RED').map((a) => a.id),
        searchedOffTab: assetsForTab(assets, 'character', 'red').map((a) => a.id),
        emptyOnEmptyProject: Object.fromEntries(
          ASSET_TABS.map((tab) => [tab.id, assetTabEmpty(tab.id)])),
        emptySearch: assetTabEmpty('prop', 'zzz'),
      }));
    """)

    per = sorted_out["per"]
    assert per["all"] == ["a1", "a2", "a3", "a4", "a5", "a6", "a7"]
    assert per["character"] == ["a1"]
    assert per["setting"] == ["a2"]
    assert per["prop"] == ["a3"]
    assert per["style"] == ["a4"]
    # The three kinds the Director did not name, together, under one honest heading.
    assert per["media"] == ["a5", "a6", "a7"]
    # The clips tab shows no assets at all: it is the take library.
    assert per["clips"] == []

    # The search box is case-insensitive and applies within the tab, not across it.
    assert sorted_out["searched"] == ["a3"]
    assert sorted_out["searchedOffTab"] == []

    # Every tab says something specific when it is empty. "No matching assets" under a tab a
    # Director deliberately opened reads as a panel that failed to load.
    messages = sorted_out["emptyOnEmptyProject"]
    titles = {tab: message["title"] for tab, message in messages.items()}
    assert len(set(titles.values())) == len(titles), titles
    for tab, message in messages.items():
        assert message["title"] and message["hint"], tab
    assert titles["clips"] == "No clips yet"
    assert "H3 take" in messages["clips"]["hint"]
    assert titles["character"] == "No characters yet"
    assert titles["media"] == "No media yet"
    # A search that matched nothing says so, and says how to get back.
    assert "zzz" in sorted_out["emptySearch"]["title"]
    assert "search" in sorted_out["emptySearch"]["hint"].lower()


def test_the_assets_panel_shows_exactly_one_pane_per_subtab():
    """The Director's report, executed: "the generated clips are eating up all the room".

    They were drawn *under* the asset grid, in the same scrolling panel, so thirty-three takes
    pushed the sorted sections out of view. What has to be true now is that exactly one of the two
    panes is on screen at a time -- a claim about the running panel, which is why `renderAssets` is
    executed here rather than read.
    """
    panes = run_workspace("""
      state.project = {
        id: 'p1', song: null, messages: [], shots: [{ id: 'shot_a', start: 0, duration: 5 }],
        assets: [
          { id: 'a1', kind: 'character', name: 'Lucy', source: 'flux', path: '', prompt_id: '' },
          { id: 'a2', kind: 'image', name: 'Reference still', source: 'upload',
            path: 'media/x.png', prompt_id: '' },
        ],
        jobs: [
          { id: 'j1', kind: 'h3', status: 'complete', target_id: 'shot_a', seed: 3,
            output_files: ['out/shot_a-h3_00001-audio.mp4'] },
        ],
      };
      const show = (tab) => {
        state.assetTab = tab;
        app.renderAssets();
        return {
          gridHidden: at('#asset-grid').hidden === true,
          clipsHidden: at('#clips-library').hidden === true,
          grid: at('#asset-grid').innerHTML,
          clips: at('#clips-library').innerHTML,
        };
      };
      console.log(JSON.stringify({
        all: show('all'),
        characters: show('character'),
        props: show('prop'),
        media: show('media'),
        clips: show('clips'),
      }));
    """)

    # Every asset tab shows the grid and hides the clips; the clips tab does the opposite. Exactly
    # one, on every tab -- neither both (the report) nor neither (a blank panel).
    for tab in ("all", "characters", "props", "media"):
        assert panes[tab]["gridHidden"] is False, tab
        assert panes[tab]["clipsHidden"] is True, (
            f"the clips library is still on screen under the {tab} tab, which is the Director's "
            "report unfixed"
        )
    assert panes["clips"]["gridHidden"] is True
    assert panes["clips"]["clipsHidden"] is False

    # ...and the panes really hold what their tab promises.
    assert "Lucy" in panes["all"]["grid"] and "Reference still" in panes["all"]["grid"]
    assert "Lucy" in panes["characters"]["grid"]
    assert "Reference still" not in panes["characters"]["grid"]
    assert "Reference still" in panes["media"]["grid"]
    assert "Lucy" not in panes["media"]["grid"]
    # An empty tab says so honestly rather than looking broken.
    assert "No props yet" in panes["props"]["grid"]
    # The clips pane is built whichever tab is showing -- `hidden` decides what is seen, so the
    # count on the tab and the rows behind it are always the same list.
    assert "shot_a-h3_00001-audio.mp4" in panes["all"]["clips"]
    assert "Generated clips" in panes["clips"]["clips"]


# ==========================================================================================
# The four recorded interaction defects cleared on 2026-08-21, plus the Director's ruling on
# the seed randomizer's scope. Every one of these is an *interaction* claim, so the browser
# harnesses carry the load; what is executed here is the deciding logic and the markup each
# render actually produces, which is the half a source read cannot settle.
# ==========================================================================================


def test_the_clips_tab_decides_playability_from_health_and_never_guesses():
    """Three answers, not two: online, offline, and "this browser has not been told".

    Saying "ComfyUI is offline" on a browser that has no health answer at all would be
    inventing a fact about someone else's process, which is the one thing the honest-status
    convention in this repository forbids.
    """
    states = run_module("""
      import { CLIP_OFFLINE_TITLE, CLIP_UNKNOWN_TITLE, COMFY_DEFAULT_URL, clipPreviewState }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        online: clipPreviewState({ comfy: { online: true, url: 'http://127.0.0.1:8188' } }),
        offline: clipPreviewState({ comfy: { online: false, url: 'http://127.0.0.1:9/' } }),
        nothing: clipPreviewState(null),
        empty: clipPreviewState({}),
        offlineTitle: CLIP_OFFLINE_TITLE,
        unknownTitle: CLIP_UNKNOWN_TITLE,
        fallbackUrl: COMFY_DEFAULT_URL,
      }));
    """)

    assert states["online"]["playable"] is True
    assert states["online"]["status"] == "online"
    # Nothing is said on a card that can play; the note is the offline tab's own line.
    assert states["online"]["note"] == ""

    assert states["offline"]["playable"] is False
    assert states["offline"]["status"] == "offline"
    assert states["offline"]["title"] == states["offlineTitle"]
    # The refusal names the address that was tried, because "offline" without one is a claim a
    # Director cannot check against the ComfyUI they started.
    assert "http://127.0.0.1:9/" in states["offline"]["note"]
    # ...and it is precise about what is lost: the current take still plays from disk.
    for note in (states["offline"]["note"], states["nothing"]["note"]):
        assert "currently points at is served by this application" in note, note
        assert "earlier take" in note.lower(), note

    # No health answer is not the same fact as a health answer saying no.
    for unknown in ("nothing", "empty"):
        assert states[unknown]["playable"] is False, unknown
        assert states[unknown]["status"] == "unknown", unknown
        assert states[unknown]["title"] == states["unknownTitle"], unknown
        assert "offline" not in states[unknown]["note"], unknown
    assert states["nothing"]["url"] == states["fallbackUrl"]


def test_a_clip_card_is_served_by_this_application_whenever_it_can_be():
    """The current take needs no ComfyUI; an earlier one does. Decided per card.

    `GET /api/projects/{id}/shots/{shot}/take` resolves the shot's own `latest_output` under
    `settings.comfy_root / "output"` **on disk** and streams it — it is what the Monitor plays,
    and no ComfyUI process is involved. It takes ids and deliberately no path, so it can serve
    exactly one take per shot. Everything earlier is addressable only by path, and the only thing
    that serves a path is ComfyUI's `/view`.
    """
    faces = run_module("""
      import { clipCardFace, clipPreviewState }
        from './src/music_video_producer/web/assets/api.js';
      const project = { id: 'p1', shots: [
        { id: 'shot_a', start: 0, duration: 5,
          latest_output: 'music-video-producer/p1/shots/shot_a-h3_00002-audio.mp4' },
      ] };
      const current = { file: 'music-video-producer/p1/shots/shot_a-h3_00002-audio.mp4', shotId: 'shot_a' };
      const earlier = { file: 'music-video-producer/p1/shots/shot_a-h3_00001-audio.mp4', shotId: 'shot_a' };
      const orphan = { file: 'music-video-producer/p1/shots/shot_gone-h3_00001-audio.mp4', shotId: 'shot_gone' };
      const online = clipPreviewState({ comfy: { online: true, url: 'http://127.0.0.1:8188' } });
      const offline = clipPreviewState({ comfy: { online: false, url: 'http://127.0.0.1:9/' } });
      console.log(JSON.stringify({
        currentOnline: clipCardFace(project, current, online),
        currentOffline: clipCardFace(project, current, offline),
        earlierOnline: clipCardFace(project, earlier, online),
        earlierOffline: clipCardFace(project, earlier, offline),
        orphanOffline: clipCardFace(project, orphan, offline),
      }));
    """)

    # The current take plays whatever ComfyUI is doing, and through this application's own route.
    for when in ("currentOnline", "currentOffline"):
        assert faces[when]["playable"] is True, when
        assert faces[when]["via"] == "app", when
        assert faces[when]["url"].startswith("/api/projects/p1/shots/shot_a/take?v="), faces[when]
        assert "127.0.0.1:8188" not in faces[when]["url"], (
            (
                "the current take is being fetched from ComfyUI when this application can serve "
                "it from disk"
            ),
            faces[when],
        )

    # An earlier take goes through ComfyUI when it is there...
    assert faces["earlierOnline"]["playable"] is True
    assert faces["earlierOnline"]["via"] == "comfy"
    assert "/view?filename=" in faces["earlierOnline"]["url"]

    # ...and says so, rather than 404ing, when it is not.
    for when in ("earlierOffline", "orphanOffline"):
        assert faces[when]["playable"] is False, when
        assert faces[when]["via"] == "", when
        assert faces[when]["url"] == "", when
        assert "ComfyUI offline" in faces[when]["title"], when


def test_the_clips_tab_draws_an_honest_card_instead_of_a_broken_video_when_comfyui_is_down():
    """The Director's report (2026-08-21): the Clips library "goes blank" with ComfyUI down.

    Executed rather than read, because the claim is about what the tab *paints*: every card
    pointed a `<video>` at ComfyUI's `/view`, so all thirty-three 404'd at once. What has to be
    true now is that the current take still plays from this application's own route, that no
    video element is created for one that cannot be shown, that the take's own filename survives
    into the card either way, and that the one action there is -- ask again -- is on screen.
    """
    drawn = run_workspace("""
      state.project = {
        id: 'p1', song: null, messages: [], assets: [],
        shots: [{ id: 'shot_a', start: 0, duration: 5, prompt: 'The corridor.',
                  latest_output: 'music-video-producer/p1/shots/shot_a-h3_00002-audio.mp4' }],
        jobs: [
          { id: 'j1', kind: 'h3', status: 'complete', target_id: 'shot_a', seed: 3,
            output_files: ['music-video-producer/p1/shots/shot_a-h3_00001-audio.mp4'] },
          { id: 'j2', kind: 'h3', status: 'complete', target_id: 'shot_a', seed: 4,
            output_files: ['music-video-producer/p1/shots/shot_a-h3_00002-audio.mp4'] },
        ],
      };
      state.assetTab = 'clips';
      const show = (health) => {
        state.health = health;
        app.renderAssets();
        return at('#clips-library').innerHTML;
      };
      console.log(JSON.stringify({
        offline: show({ comfy: { online: false, url: 'http://127.0.0.1:9/' } }),
        unknown: show(null),
        online: show({ comfy: { online: true, url: 'http://127.0.0.1:8188' } }),
      }));
    """)

    current = "shot_a-h3_00002-audio.mp4"
    earlier = "shot_a-h3_00001-audio.mp4"
    for state_name in ("offline", "unknown"):
        markup = drawn[state_name]
        assert "/view?filename=" not in markup, (
            f"the Clips tab still points a video element at an unreachable ComfyUI while it is "
            f"{state_name}, which is the Director's wall of broken cards unfixed"
        )
        # The current take still plays, through this application's own route.
        assert 'data-via="app"' in markup, state_name
        assert "/shots/shot_a/take?v=" in markup, state_name
        # The earlier one says why it cannot be shown, rather than 404ing.
        assert "clip-unplayable" in markup, state_name
        # Both takes' identities survive: the filename is what names the file on disk.
        assert current in markup and earlier in markup, state_name
        # ...and so does the way back to the shot that produced them.
        assert markup.count("clip-jump") == 2, state_name
        assert "clips-offline" in markup, state_name
        assert 'id="clips-recheck"' in markup, ("the tab offers no way to ask again", state_name)
        assert "Re-check ComfyUI" in markup, state_name

    # With ComfyUI answering, every take plays and there is no apology standing over a working
    # list -- and the current take *still* comes from this application rather than from ComfyUI.
    assert "/view?filename=" in drawn["online"]
    assert 'data-via="app"' in drawn["online"]
    assert 'data-via="comfy"' in drawn["online"]
    assert "clip-unplayable" not in drawn["online"]
    assert "clips-offline" not in drawn["online"]
    # The re-check is drawn in *both* states. A control that appears only once the browser
    # already knows ComfyUI is down could never be pressed by a Director whose ComfyUI stopped
    # after the page loaded: health is fetched at boot and nowhere else, so that session would go
    # on drawing broken cards with no way to ask.
    assert 'id="clips-recheck"' in drawn["online"]


def test_the_clips_recheck_asks_health_again_rather_than_polling_comfyui():
    """The only thing that knocks on ComfyUI is a press. Nothing here polls.

    A tab that re-probed on every render would be this application deciding how often to contact
    a process it is forbidden from managing, so the request is asserted to happen *on the click*
    and not before it.
    """
    asked = run_workspace(
        """
      state.project = {
        id: 'p1', song: null, messages: [], assets: [],
        shots: [{ id: 'shot_a', start: 0, duration: 5, prompt: 'The corridor.' }],
        jobs: [
          { id: 'j1', kind: 'h3', status: 'complete', target_id: 'shot_a', seed: 3,
            output_files: ['out/shot_a-h3_00001-audio.mp4'] },
        ],
      };
      state.health = { comfy: { online: false, url: 'http://127.0.0.1:9/' }, llm: {} };
      state.assetTab = 'clips';
      app.renderAssets();
      requests.length = 0;
      app.renderAssets();
      const beforeClick = requests.filter((entry) => entry.path === '/api/health').length;
      await fire('#clips-recheck:click', {});
      await flush();
      console.log(JSON.stringify({
        beforeClick,
        health: requests.filter((entry) => entry.path === '/api/health').length,
        stillOffline: at('#clips-library').innerHTML.includes('clip-unplayable'),
      }));
    """,
        responses={
            "/api/health": {
                "body": {
                    "comfy": {"online": False, "url": "http://127.0.0.1:9/"},
                    "llm": {"configured": False, "model": ""},
                }
            }
        },
    )

    assert asked["beforeClick"] == 0, (
        "the Clips tab probes ComfyUI every time it redraws; that is a poll this application does "
        "not get to decide the rate of"
    )
    assert asked["health"] == 1, asked
    # A re-check that finds nothing leaves the same honest card behind rather than blanking it.
    assert asked["stillOffline"] is True, asked


def test_attach_to_selected_shot_names_the_shot_it_will_write_to():
    """The Director's report (2026-08-21): "hard to use since cant see timeline from assets page".

    `replaceInShotsControl` solved the same problem by putting the count in its own label; this
    puts the identity in the label and the window and the intent in the line under it.
    """
    control = run_module("""
      import { ATTACH_LABEL_UNSELECTED, attachToShotControl, shotWindowLabel }
        from './src/music_video_producer/web/assets/api.js';
      const project = { id: 'p1', shots: [
        { id: 'shot_a', start: 0, duration: 5,
          prompt: 'Lucy walks the service corridor, hand on the rail' },
        { id: 'shot_b', start: 5, duration: 4.5, prompt: '',
          citations: [{ asset_id: 'asset_lucy', role: 'reference', order: 0 }] },
      ] };
      console.log(JSON.stringify({
        none: attachToShotControl(project, null, 'asset_lucy', 'Lucy'),
        missing: attachToShotControl(project, 'shot_gone', 'asset_lucy', 'Lucy'),
        live: attachToShotControl(project, 'shot_a', 'asset_lucy', 'Lucy'),
        cited: attachToShotControl(project, 'shot_b', 'asset_lucy', 'Lucy'),
        noIntent: attachToShotControl(project, 'shot_b', 'asset_other', 'Corridor'),
        unselectedLabel: ATTACH_LABEL_UNSELECTED,
        window: shotWindowLabel({ start: 5, duration: 4.5 }),
      }));
    """)

    # No selection: shut, and the caption is the reason rather than a shrug.
    for empty in ("none", "missing"):
        assert control[empty]["disabled"] is True, empty
        assert control[empty]["label"] == control["unselectedLabel"], empty
        assert "No shot is selected" in control[empty]["caption"], empty
        assert "Timeline" in control[empty]["caption"], empty

    # A selection: the label carries the number the timeline paints on the clip, and the caption
    # carries the id, the window and the opening of the intent -- the three things the Assets
    # panel cannot show.
    assert control["live"]["disabled"] is False
    assert control["live"]["label"] == "Attach to SHOT 01"
    assert "SHOT 01 (shot_a)" in control["live"]["caption"]
    assert "0.00–5.00 s" in control["live"]["caption"]
    assert "Lucy walks the service corridor" in control["live"]["caption"]
    assert "Lucy" in control["live"]["title"] and "SHOT 01" in control["live"]["title"]

    # Already cited: shut, because the click was a no-op that toasted success all the same --
    # the "control that appears to do nothing" shape this whole thread started from.
    assert control["cited"]["disabled"] is True
    assert control["cited"]["label"] == "Attach to SHOT 02"
    assert "already cites Lucy" in control["cited"]["reason"]

    # A shot with no intent written says so rather than trailing off into an empty caption.
    assert control["noIntent"]["disabled"] is False
    assert "no creative intent written yet" in control["noIntent"]["caption"]
    assert control["window"] == "5.00–9.50 s"


def test_the_assets_inspector_draws_the_named_attach_target_and_shuts_it_with_its_reason():
    """Executed, because a control's *drawn* state is what a Director acts on.

    The offline harness cannot see a button that renders and is then covered -- the browser
    harness asserts that -- but it can prove that the panel writes the name, the window and the
    disabled state that `attachToShotControl` decided.
    """
    drawn = run_workspace("""
      state.project = {
        id: 'p1', song: null, messages: [], jobs: [],
        shots: [
          { id: 'shot_a', start: 12, duration: 5, prompt: 'The corridor, pushing in on Lucy.' },
          { id: 'shot_b', start: 17, duration: 5, prompt: 'Wider.',
            citations: [{ asset_id: 'asset_lucy', role: 'reference', order: 0 }] },
        ],
        assets: [{ id: 'asset_lucy', name: 'Lucy the singer', kind: 'character',
                   source: 'upload', path: '', prompt: '', prompt_id: '',
                   created_at: '2026-08-20T09:15:00Z' }],
      };
      state.selectedAssetId = 'asset_lucy';
      const show = (shotId) => {
        state.selectedShotId = shotId;
        app.renderAssetInspector();
        return at('#asset-inspector').innerHTML;
      };
      console.log(JSON.stringify({
        none: show(null),
        live: show('shot_a'),
        cited: show('shot_b'),
      }));
    """)

    assert 'id="attach-asset"' in drawn["none"]
    assert "Attach to selected shot" in drawn["none"]
    assert "disabled" in drawn["none"].split('id="attach-asset"', 1)[1].split("</button>", 1)[0]
    assert "No shot is selected" in drawn["none"]

    live = drawn["live"]
    assert "Attach to SHOT 01" in live
    assert "SHOT 01 (shot_a)" in live
    assert "12.00–17.00 s" in live
    assert 'id="attach-asset-target"' in live
    assert "disabled" not in live.split('id="attach-asset"', 1)[1].split("</button>", 1)[0]

    cited = drawn["cited"]
    assert "Attach to SHOT 02" in cited
    assert "disabled" in cited.split('id="attach-asset"', 1)[1].split("</button>", 1)[0]
    assert "already cites Lucy the singer" in cited


def test_split_refuses_a_window_it_cannot_halve_and_names_the_arithmetic():
    """`#split-shot` declined a window under a second and said nothing at all.

    The wording explains rather than scolds, because a 0.5 s window is a real thing the Director
    creates deliberately -- micro-cuts are legitimate, and `styles.css` deliberately draws no
    warning on the short end.
    """
    plans = run_module("""
      import { MIN_WINDOW_SECONDS, SPLIT_MINIMUM_SECONDS, splitShotPlan }
        from './src/music_video_producer/web/assets/api.js';
      const project = { id: 'p1', shots: [
        { id: 'shot_a', start: 0, duration: 0.75 },
        { id: 'shot_b', start: 0.75, duration: 1 },
        { id: 'shot_c', start: 1.75, duration: 5.042 },
      ] };
      console.log(JSON.stringify({
        min: MIN_WINDOW_SECONDS,
        least: SPLIT_MINIMUM_SECONDS,
        nothing: splitShotPlan(project, null),
        short: splitShotPlan(project, project.shots[0]),
        exactlyEnough: splitShotPlan(project, project.shots[1]),
        ordinary: splitShotPlan(project, project.shots[2]),
      }));
    """)

    assert plans["min"] == 0.5
    assert plans["least"] == 1

    assert plans["nothing"]["ok"] is False
    assert "No shot is selected" in plans["nothing"]["refusal"]

    short = plans["short"]
    assert short["ok"] is False
    assert short["halves"] == []
    # The number the Director is looking at, the number that halving produces, the floor it lands
    # under, and the number to drag past. All four, because a refusal that names none of them is
    # a refusal nobody can act on.
    assert "SHOT 01 (shot_a)" in short["refusal"]
    assert "0.75s" in short["refusal"]
    assert "0.375s" in short["refusal"]
    assert "0.5s" in short["refusal"]
    assert "past 1s" in short["refusal"]

    # Exactly twice the floor still splits: the refusal is `<`, not `<=`, and each half lands
    # exactly on the number every drag in the workspace stops at.
    assert plans["exactlyEnough"]["ok"] is True
    assert plans["exactlyEnough"]["halves"] == [
        {"start": 0.75, "duration": 0.5},
        {"start": 1.25, "duration": 0.5},
    ]

    # An ordinary window halves exactly, and the second half starts where the first one ends --
    # the same arithmetic the one-line handler did before it grew a refusal.
    ordinary = plans["ordinary"]
    assert ordinary["ok"] is True
    first, second = ordinary["halves"]
    assert first["start"] + first["duration"] == second["start"]
    assert first["duration"] == second["duration"] == 5.042 / 2


def test_the_split_button_says_its_refusal_and_writes_nothing():
    """Driven through the real handler: the toast is raised and the plan is untouched."""
    driven = run_workspace("""
      const toasts = [];
      globalThis.document.createElement = () => {
        const item = make('<toast>'); toasts.push(item); return item;
      };
      state.project = {
        id: 'p1', song: null, messages: [], assets: [], jobs: [],
        shots: [{ id: 'shot_a', start: 0, duration: 0.75, prompt: 'A micro-cut, on purpose.' }],
      };
      state.selectedShotId = 'shot_a';
      requests.length = 0;
      fire('#split-shot:click', {});
      console.log(JSON.stringify({
        toasts: toasts.map((item) => ({ text: item.textContent, kind: item.className })),
        shots: state.project.shots.length,
        duration: state.project.shots[0].duration,
        wrote: requests.filter((entry) => entry.method === 'PUT').length,
      }));
    """)

    assert driven["shots"] == 1, "the split it refused still added a shot"
    assert driven["duration"] == 0.75, "the refused split narrowed the window anyway"
    assert driven["wrote"] == 0, "a refusal wrote the shot list back"
    assert len(driven["toasts"]) == 1, driven["toasts"]
    said = driven["toasts"][0]
    assert "error" in said["kind"], said
    assert "0.75s" in said["text"] and "0.375s" in said["text"], said["text"]


def test_the_seed_randomizer_is_armed_per_shot_and_not_for_the_session():
    """The Director's ruling (2026-08-21): "it should be per shot".

    It shipped session-wide the day before. Executed here because the defect is invisible in the
    source -- one module-level boolean reads exactly like one module-level `Set` -- and shows up
    only when a second shot's inspector is drawn.
    """
    armed = run_workspace("""
      state.project = {
        id: 'p1', song: null, messages: [], assets: [], jobs: [],
        shots: [
          { id: 'shot_a', start: 0, duration: 5, prompt: 'One.', seed: 12, status: 'complete' },
          { id: 'shot_b', start: 5, duration: 5, prompt: 'Two.', seed: 34, status: 'complete' },
        ],
      };
      const checked = (shotId) => {
        state.selectedShotId = shotId;
        app.renderShotInspector();
        // `String.split(sep, limit)` truncates the *result* in JavaScript rather than limiting
        // the number of splits as it does in Python, so no limit is passed here.
        return at('#shot-inspector').innerHTML
          .split('id="shot-seed-randomize"')[1].split('>')[0].includes('checked');
      };
      const before = { a: checked('shot_a'), b: checked('shot_b') };
      // Tick it on shot A, through the control's own handler.
      state.selectedShotId = 'shot_a';
      app.renderShotInspector();
      at('#shot-seed-randomize').checked = true;
      fire('#shot-seed-randomize:change', { target: at('#shot-seed-randomize') });
      const after = { a: checked('shot_a'), b: checked('shot_b') };
      const rolledA = state.project.shots[0].seed;
      const heldB = state.project.shots[1].seed;
      // Typing a seed by hand on B must not disarm A.
      state.selectedShotId = 'shot_b';
      app.renderShotInspector();
      at('#shot-seed').value = '4242';
      fire('#shot-seed:change', {});
      const afterTyping = { a: checked('shot_a'), b: checked('shot_b') };
      console.log(JSON.stringify({ before, after, afterTyping, rolledA, heldB }));
    """)

    assert armed["before"] == {"a": False, "b": False}, "the randomizer ships armed"
    assert armed["after"]["a"] is True, (
        "ticking the randomizer did not arm the shot it was ticked on"
    )
    assert armed["after"]["b"] is False, (
        "ticking the randomizer on one shot armed another one -- this is the Director's report "
        'unfixed: "Clicking randomize integer on a shot toggles it for all shots"'
    )
    # Ticking rolls for the shot it was ticked on, and only that shot's number moves.
    assert armed["rolledA"] != 12 and 1 <= armed["rolledA"] <= 99999, armed
    assert armed["heldB"] == 34, "ticking on one shot rolled a seed for another"
    # A hand-typed number is a statement about the shot it was typed on.
    assert armed["afterTyping"] == {"a": True, "b": False}, armed["afterTyping"]


def test_the_randomizers_armed_shots_are_forgotten_when_the_project_changes():
    """The music lock's lifecycle, followed rather than reinvented -- and for its reasons.

    Ids collide across projects, so a set carried into another project would arm the randomizer on
    a shot nobody has looked at; and the clear is gated on the project actually *changing*, because
    most callers of `loadProject` are refreshes of the project already on screen and unticking a
    box there would be the control fighting the Director mid-gesture.

    Asserted the same way `test_the_lock_is_session_state_and_never_a_field_on_the_shot` asserts
    it, and no more strongly: the guard is one line inside a function the stub DOM cannot drive
    through a project switch without a whole second project's worth of canned replies.
    """
    models = Path("src/music_video_producer/models.py").read_text(encoding="utf-8")
    for spelling in ("randomize_seed", "seed_randomize", "randomise_seed"):
        assert spelling not in models, f"the randomizer became a model field ({spelling})"
    load = app_js_block("async function loadProject(id) {", "\nasync function")
    assert "randomizeSeedShots.clear();" in load, (
        "the randomizer's armed shots survive a project change, so they arm shots in a plan the "
        "Director has not opened"
    )
    cleared = load.split("randomizeSeedShots.clear();", 1)[0]
    # Inside the same "did the project actually change" guard the music lock uses -- not on every
    # refresh. Read back through the block rather than the line before it, because the two clears
    # sit together with a comment between them.
    assert "documentConsentClearedOnLoad(state.project?.id, id)" in cleared
    assert "unlockedFromMusic.clear();" in cleared, (
        "the two session sets are no longer cleared together, so one of them is outside the guard"
    )


def test_the_retake_reads_the_toggle_of_the_shot_it_is_requeueing():
    """`nextRenderSeed` stays the one branch; what changed is which shot's flag it is handed."""
    source = APP_JS.read_text(encoding="utf-8")
    calls = [line for line in source.splitlines() if "nextRenderSeed(" in line]
    assert len(calls) == 1, ("there is more than one place a queued retake's seed moves", calls)
    assert "randomizeSeedFor(shot.id)" in calls[0], calls[0]
    # No module-level boolean survives anywhere: a session-wide flag beside a per-shot set is two
    # answers to one question.
    assert "let randomizeSeed = false" not in source
    assert "const randomizeSeedShots = new Set()" in source


def test_the_randomize_help_states_the_per_shot_scope_the_director_ruled_on():
    help_text = run_module("""
      import { RANDOM_SEED_HELP } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify(RANDOM_SEED_HELP));
    """)
    assert "per shot" in help_text
    assert "arms this shot and no other" in help_text
    # The three things that were true before and still are.
    assert "Render again" in help_text
    assert "Mark ready" in help_text
    assert "Generate All" in help_text


def test_the_resize_handle_outranks_every_clip_body_without_reordering_the_clips():
    """The Director's overlap report (2026-08-21), and the ruling it must not break.

    A clip that overlaps its later neighbour has that neighbour painted over its right edge, so
    the right handle cannot be grabbed. The fix raises the *handle*; which clip body paints on
    top is untouched, because that is the 2026-08-20 layering ruling. Whether the handle is
    genuinely hit-testable at a real overlap is a browser claim and is asserted in
    `tests/e2e_clip_overlap_and_split.py`; this is the stylesheet half.
    """
    css = STYLES_CSS.read_text(encoding="utf-8")
    handle = re.search(r"^\.resize-handle \{([^}]*)\}", css, re.MULTILINE)
    assert handle, "styles.css no longer declares the resize handle"
    assert "z-index: 2" in handle.group(1), (
        "the resize handle no longer outranks the clip bodies, so an overlapped right edge is "
        "unreachable again"
    )

    # The clip bodies stay unranked. A z-index on `.shot-clip` -- or anything that makes one a
    # stacking context (`transform`, `filter`, `opacity` under 1, `isolation`) -- would either
    # change which picture is in front or trap the handle inside its own clip, and both break the
    # ruling this fix was written around.
    for rule in re.findall(r"^\.shot-clip[^{]*\{([^}]*)\}", css, re.MULTILINE):
        for forbidden in ("z-index", "isolation", "transform", "filter"):
            assert forbidden not in rule, (
                (
                    f"`.shot-clip` now sets {forbidden}, which changes which clip paints on top "
                    "or makes the clip a stacking context the handle cannot escape"
                ),
                rule,
            )


# ---------------------------------------------------------------------------------------------
# Who sings the song, and which of them sings each line (pass 1).
#
# The client mirrors three server things: the vocal-type table, the `(S1)` sheet parser, and the
# writer that edits one line of it. All three are held equal to the server's here, and the
# dropdown itself is *rendered and driven* against the stub DOM rather than grepped — a per-line
# select drawn once per lyric line has no fixed id, so the only way to prove one exists and does
# something is to find it the way the panel's own code does and fire it.
# ---------------------------------------------------------------------------------------------

# One fixture, parsed by both languages, so "the two agree" is measured on the same bytes. CRLF,
# an indented line, a blank, a `[Tag]` header, a mark that reads, a mark that does not, and an
# ordinary parenthetical that must not be mistaken for one.
TAG_FIXTURE = (
    "[Verse]\r\n"
    "(S1) I don't care if you track me down\r\n"
    "  (s2) Like an animal\r\n"
    "\r\n"
    "(S1, S2) Tie me down\r\n"
    "[Chorus]\r\n"
    "(S9) past the bound\r\n"
    # An UNCLOSED mark, which is the only unreadable shape that never matches the mark pattern at
    # all and so reaches its answer through the suspect branch rather than through the two
    # re-validations after it. A fixture without one let a client that silently answered
    # "untagged" there pass unnoticed — found by the mutation sweep, not by the test.
    "(S1 unclosed\r\n"
    "(she said) quietly\r\n"
)


def test_the_client_offers_exactly_the_vocal_types_and_line_tags_the_server_declares():
    """One table, mirrored. A client offering a cast the server has no slots for would draw a
    dropdown whose every choice the route refuses; one offering a line tag the server does not
    know would write a mark the sheet parser cannot read back."""
    mirrored = run_module("""
      import { VOCAL_TYPES, CHARACTER_SLOT_LIMIT, INSTRUMENTAL_NOTE, lineTagOptions }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        types: VOCAL_TYPES.map((entry) => ({
          value: entry.value, label: entry.label, slots: entry.slots,
          tags: lineTagOptions(entry.value).map((tag) => [tag.label, tag.slots]),
        })),
        limit: CHARACTER_SLOT_LIMIT,
        note: INSTRUMENTAL_NOTE,
      }));
    """)

    assert [entry["value"] for entry in mirrored["types"]] == list(VOCAL_TYPE_SPECS)
    for entry in mirrored["types"]:
        spec = VOCAL_TYPE_SPECS[entry["value"]]
        assert entry["label"] == spec.label
        assert entry["slots"] == list(spec.slots)
        assert entry["tags"] == [[tag.label, list(tag.slots)] for tag in spec.line_tags]
    assert mirrored["limit"] == CHARACTER_SLOT_LIMIT
    # The instrumental consequence is stated once and quoted twice; a consequence worded two ways
    # is one the Director cannot trust.
    assert mirrored["note"] == INSTRUMENTAL_NOTE


def test_the_client_reads_and_writes_the_sheet_exactly_as_the_server_does():
    """Both parsers over one fixture, and both writers over one edit.

    A client that read `(s2)` where the server did not would show the Director a dropdown state
    their sheet does not carry; one that wrote a mark the server parses differently would store a
    tag that reads back as something else.
    """
    parsed = run_module(f"""
      import {{ lyricLineTags, tagLyricLine }} from './src/music_video_producer/web/assets/api.js';
      const sheet = {json.dumps(TAG_FIXTURE)};
      console.log(JSON.stringify({{
        lines: lyricLineTags(sheet).map((line) => [line.index, line.raw, line.slots, line.taggable, line.unreadable]),
        written: tagLyricLine(sheet, 2, [1, 2]),
        cleared: tagLyricLine(sheet, 1, []),
        refused: [[[0], 'slot 0'], [[99], 'slot past the bound'], [[1, 1], 'one singer twice']]
          .filter(([slots]) => {{ try {{ tagLyricLine(sheet, 1, slots); return false; }} catch {{ return true; }} }})
          .map(([, name]) => name),
      }}));
    """)

    assert parsed["lines"] == [
        [line.index, line.raw, list(line.slots), line.taggable, line.unreadable]
        for line in lyric_line_tags(TAG_FIXTURE)
    ]
    # Case-insensitive, exactly as `h3_prompt._SPEAKER` is: a Director typing `(s2)` by hand has
    # not made a mistake.
    assert parsed["lines"][2][2] == [2]
    # Both unreadable shapes are reported by both and repaired by neither: a slot past the bound
    # (which matches the mark pattern and fails its re-validation) and an unclosed bracket (which
    # never matches at all).
    assert parsed["lines"][6][4] is True and parsed["lines"][6][1] == "(S9) past the bound"
    assert parsed["lines"][7][4] is True and parsed["lines"][7][1] == "(S1 unclosed"
    # And the ordinary parenthetical is not dragged into the notation.
    assert parsed["lines"][8][4] is False and parsed["lines"][8][2] == []

    assert parsed["written"] == tag_lyric_line(TAG_FIXTURE, 2, (1, 2))
    # Round-trip closure, refused on the same two conditions on both sides: neither writer may
    # store a mark its own reader would then report as unreadable.
    assert parsed["refused"] == ["slot 0", "slot past the bound", "one singer twice"]
    assert parsed["cleared"] == tag_lyric_line(TAG_FIXTURE, 1, ())
    # The writer touched one line and normalised no separator.
    assert parsed["written"].count("\r\n") == TAG_FIXTURE.count("\r\n")


def test_the_per_line_dropdown_is_drawn_only_for_a_cast_and_writes_into_the_sheet():
    """The dropdown, rendered and driven against the stub DOM.

    Three things no source read can prove: that a solo song is offered NO per-line control at all
    (the Director's "an unnecessary dropdown on every line is noise"), that a duet is offered one
    per sung line seeded from the sheet, and that changing one **edits the lyric sheet in the box**
    rather than storing a tag beside it. The last is the whole storage decision, executed.
    """
    rendered = run_workspace("""
      const sheet = '[Verse]\\nAlpha line\\nBravo line\\n\\n[Chorus]\\n(S1) Charlie line\\n';
      const song = { title: 'Duet', source: 'imported', path: 'media/songs/000-m.wav', duration: 60, lyrics: sheet, caption: '', vocal_type: 'female' };
      const read = () => ({
        hidden: at('#lyric-tagging').hidden,
        markup: at('#lyric-tagging').innerHTML,
        selected: at('#song-vocal-type').value,
        options: at('#song-vocal-type').innerHTML,
        note: at('#song-vocal-note').textContent,
        lyrics: at('#song-lyrics').value,
      });
      state.project = { id: 'p1', shots: [], jobs: [], song };
      state.songContextDirty = false;
      app.renderSong();
      const solo = read();

      state.project = { id: 'p1', shots: [], jobs: [], song: { ...song, vocal_type: 'duet' } };
      state.songContextDirty = false;
      app.renderSong();
      const duet = read();

      // The Director picks "Both" on the second sung line. The handler is found the way the
      // panel's own code finds it, and fired.
      at('.lyric-line-tag[2]').value = '1,2';
      fire('.lyric-line-tag[2]:change');
      const tagged = read();
      // Re-read after the redraw: the select must come back showing what was written.
      const reselected = at('.lyric-line-tag[2]').value;
      const dirty = state.songContextDirty;

      // And the Director edits the sheet afterwards, deleting the line above the tagged one.
      at('#song-lyrics').value = at('#song-lyrics').value.replace('Alpha line\\n', '');
      // Redrawn through the exported render rather than through the input event: the stub
      // DOM keeps one listener per selector, and `#song-lyrics` carries three in the real
      // workspace, so which one a fire reaches is an artefact of bind order.
      app.renderVocalTagging();
      const afterEdit = read();

      state.project = { id: 'p1', shots: [], jobs: [], song: { ...song, vocal_type: 'instrumental' } };
      state.songContextDirty = false;
      app.renderSong();
      const instrumental = read();
      console.log(JSON.stringify({ solo, duet, tagged, reselected, dirty, afterEdit, instrumental }));
    """)

    # A solo song: the select carries every type and the per-line region is not drawn at all.
    assert rendered["solo"]["selected"] == "female"
    assert rendered["solo"]["hidden"] is True
    assert rendered["solo"]["markup"] == ""
    for spec in VOCAL_TYPE_SPECS.values():
        assert f">{spec.label}</option>" in rendered["solo"]["options"]

    # A duet: one select per SUNG line — three of them — and none on the blank or the two
    # `[Tag]` headers.
    assert rendered["duet"]["hidden"] is False
    assert rendered["duet"]["markup"].count('class="lyric-line-tag"') == 3
    for tag in VOCAL_TYPE_SPECS["duet"].line_tags:
        assert f">{tag.label}</option>" in rendered["duet"]["markup"]
    # Seeded from the sheet: the already-marked line comes up on Char 1.
    assert '<option value="1" selected>Char 1</option>' in rendered["duet"]["markup"]

    # The change wrote into the lyric sheet, and into nothing else.
    assert rendered["tagged"]["lyrics"] == (
        "[Verse]\nAlpha line\n(S1, S2) Bravo line\n\n[Chorus]\n(S1) Charlie line\n"
    )
    assert rendered["reselected"] == "1,2", "the dropdown did not read back what it wrote"
    # Unsaved, exactly as typing is: the tag is lyrics, and Save song context is what stores it.
    assert rendered["dirty"] is True

    # **The drift case, in the browser.** Deleting the line above moves nothing: the tag is in the
    # line, so the row that reads `(S1, S2)` is still the row whose words are "Bravo line".
    assert rendered["afterEdit"]["lyrics"] == (
        "[Verse]\n(S1, S2) Bravo line\n\n[Chorus]\n(S1) Charlie line\n"
    )
    assert rendered["afterEdit"]["markup"].count('class="lyric-line-tag"') == 2
    assert '<option value="1,2" selected>Both</option>' in rendered["afterEdit"]["markup"]
    # And the redraw really is wired to the box, which the stub DOM's one-listener-per-
    # selector map cannot demonstrate by firing.
    assert '$("#song-lyrics").addEventListener("input", renderVocalTagging);' in (
        APP_JS.read_text(encoding="utf-8")
    )

    # Instrumental: no per-line control, and the consequence said where the declaration is made.
    assert rendered["instrumental"]["hidden"] is True
    assert rendered["instrumental"]["note"] == INSTRUMENTAL_NOTE
    assert rendered["solo"]["note"] == ""


def test_the_song_workspace_markup_carries_the_vocal_controls():
    """The three ids `renderVocalTagging` writes into, and the select shipped empty and disabled:
    the options come from api.js's table, so markup that hard-coded them could offer a cast the
    server does not know."""
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert '<select id="song-vocal-type" disabled></select>' in markup
    assert 'id="song-vocal-note"' in markup
    assert '<div class="lyric-tagging" id="lyric-tagging" hidden></div>' in markup
    # Not one vocal type is spelled into the template.
    for spec in VOCAL_TYPE_SPECS.values():
        assert f'<option value="{spec.label}"' not in markup


def test_the_character_slot_select_shuts_a_slot_another_asset_holds():
    """`characterSlotPlan`, executed. Offered for a character and for nothing else, and a slot
    another asset already holds is shown-and-shut rather than hidden — a Director looking for
    "why can I not pick S1" is owed the name of the asset that has it, which is what the route's
    own refusal says too."""
    plans = run_module("""
      import { characterSlotPlan } from './src/music_video_producer/web/assets/api.js';
      const project = { song: { vocal_type: 'duet' }, assets: [
        { id: 'a1', name: 'Singer One', kind: 'character', character_slot: 1 },
        { id: 'a2', name: 'Singer Two', kind: 'character', character_slot: 0 },
        { id: 'a3', name: 'Chrome Mic', kind: 'prop', character_slot: 0 },
      ] };
      console.log(JSON.stringify({
        holder: characterSlotPlan(project, project.assets[0]),
        other: characterSlotPlan(project, project.assets[1]),
        prop: characterSlotPlan(project, project.assets[2]),
        undeclared: characterSlotPlan({ song: { vocal_type: 'unstated' }, assets: [] },
                                      { id: 'a9', kind: 'character', character_slot: 0 }),
      }));
    """)

    # A slot names a singer, so a prop is offered none at all — the route refuses one by name.
    assert plans["prop"] is None
    # The declared type's own slots, plus 0 for "not one of the singers".
    assert plans["other"]["options"] == [0, *VOCAL_TYPE_SPECS["duet"].slots]
    assert plans["other"]["taken"] == {"1": "Singer One"}
    # The asset holding a slot does not see its own slot as taken, so it can re-assert it.
    assert plans["holder"]["slot"] == 1 and plans["holder"]["taken"] == {}
    # With nothing declared the full bound is offered, so the cast can be slotted in either order.
    assert plans["undeclared"]["options"] == [0, *range(1, CHARACTER_SLOT_LIMIT + 1)]


def test_the_client_calls_the_one_writer_for_each_new_field():
    """Both fields have exactly one route, and the client must not reach either through
    `saveProject` — that route re-adopts both and would silently drop the write."""
    calls = run_module("""
      import { api } from './src/music_video_producer/web/assets/api.js';
      const seen = [];
      globalThis.fetch = (path, options) => { seen.push([path, options.method, options.body]);
        return Promise.resolve({ ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => ({}) }); };
      await api.saveVocalType('p1', 'duet');
      await api.saveCharacterSlot('p1', 'a1', 2);
      await api.renameAsset('p1', 'a1', 'Lucy');
      console.log(JSON.stringify(seen));
    """)

    assert calls == [
        ["/api/projects/p1/song/vocal-type", "PUT", '{"vocal_type":"duet"}'],
        ["/api/projects/p1/assets/a1/character-slot", "PUT", '{"character_slot":2}'],
        ["/api/projects/p1/assets/a1/name", "PUT", '{"name":"Lucy"}'],
    ]


# ----------------------------------------------------------------------------------------------
# Renaming an asset (2026-08-22).
#
# The Director's fix for the internal label leaking into shot prose. The client half is the
# anchor editor's shape with one rule inverted: an empty box is unsavable rather than a clear.
# ----------------------------------------------------------------------------------------------


def test_the_name_editor_decides_every_state_from_one_executed_rule():
    """Executed, kind by kind and state by state, and the bound is compared to the route's.

    Offered for **every** kind including `audio`, which is where this parts from the anchor: a
    sound has no appearance but it does have a name. And the empty box is not a clear — the
    route refuses a blank name by name, so the button must not be able to send one.
    """
    kinds = list(get_args(AssetKind))
    executed = run_module(f"""
      import {{ ASSET_NAME_LIMIT, assetNamePlan }}
        from './src/music_video_producer/web/assets/api.js';
      const kinds = {json.dumps(kinds)};
      const offered = {{}};
      for (const kind of kinds) {{
        offered[kind] = assetNamePlan({{ id: 'a', kind, name: 'Anything' }}) !== null;
      }}
      const stored = {{ id: 'a', kind: 'character', name: 'HarderFaster \\u00b7 multiview' }};
      console.log(JSON.stringify({{
        limit: ASSET_NAME_LIMIT,
        offered,
        nothingSelected: assetNamePlan(null),
        untouched: assetNamePlan(stored),
        whitespaceOnly: assetNamePlan(stored, '  HarderFaster \\u00b7 multiview  '),
        renamed: assetNamePlan(stored, 'Lucy'),
        emptied: assetNamePlan(stored, '   '),
        overLong: assetNamePlan(stored, 'z'.repeat(ASSET_NAME_LIMIT + 1)),
        atLimit: assetNamePlan(stored, 'z'.repeat(ASSET_NAME_LIMIT)),
      }}));
    """)

    assert executed["limit"] == ASSET_NAME_LIMIT
    assert all(executed["offered"][kind] for kind in kinds)
    assert executed["nothingSelected"] is None

    assert executed["untouched"]["stored"] == "HarderFaster · multiview"
    assert executed["untouched"]["savable"] is False
    # Trailing whitespace is not an edit, because the route trims before it stores.
    assert executed["whitespaceOnly"]["savable"] is False

    # The whole name is replaced — the promotion suffix is not preserved by either side.
    assert executed["renamed"]["savable"] is True
    assert executed["renamed"]["draft"] == "Lucy"

    # Emptying the box is a change and still unsavable, and the count says why in words rather
    # than only in a colour.
    assert executed["emptied"]["changed"] is True
    assert executed["emptied"]["savable"] is False
    assert "cannot be empty" in executed["emptied"]["count"]

    assert executed["overLong"]["savable"] is False
    assert "too long to save" in executed["overLong"]["count"]
    assert executed["atLimit"]["savable"] is True


def test_the_inspector_draws_a_rename_box_and_saves_it_through_the_one_route():
    """Rendered and clicked, not read as text — `renderAssetInspector`'s own markup.

    The box sits above the appearance anchor and the read-only generation prompt: the name is
    what the rest of the panel is about, and it is the field the Director renames to keep an
    internal label out of shot prose. The save goes to the dedicated route carrying the trimmed
    name and nothing else; folded into the whole-project PUT it would be silently re-adopted.
    """
    fired = run_workspace("""
      state.project = { id: 'p1', shots: [], jobs: [], assets: [{
        id: 'a1', kind: 'character', path: 'out/a.png', name: 'HarderFaster \\u00b7 multiview',
        source: 'krea-multiview', prompt: 'a woman in a blue dress',
        consistency_prompt: '', created_at: '2026-08-20T00:00:00Z',
      }] };
      state.selectedAssetId = 'a1';
      app.renderAssetInspector();
      const markup = at('#asset-inspector').innerHTML;

      const before = markup.includes('id="save-asset-name" disabled');
      at('#asset-name').value = '   ';
      await fire('#asset-name:input', {});
      const afterEmptying = {
        disabled: at('#save-asset-name').disabled,
        count: at('#asset-name-count').textContent,
      };
      at('#asset-name').value = '  Lucy  ';
      await fire('#asset-name:input', {});
      const afterTyping = { disabled: at('#save-asset-name').disabled };
      requests.length = 0;
      await fire('#save-asset-name:click', {});
      const saved = requests.map((sent) => ({ path: sent.path, method: sent.method, body: sent.body }));

      console.log(JSON.stringify({
        drawn: markup.includes('id="asset-name"'),
        holdsStored: markup.includes('HarderFaster'),
        nameBeforeAnchor: markup.indexOf('id="asset-name"') < markup.indexOf('Generation prompt'),
        noMaxlength: !markup.includes('maxlength'),
        before, afterEmptying, afterTyping, saved,
        adopted: state.project.id,
      }));
    """, responses={
        "/api/projects/p1/assets/a1/name": {
            "body": {
                "project": {"id": "p2", "shots": [], "jobs": [], "assets": []},
                "name": "Lucy",
                "previous": "HarderFaster · multiview",
                "prompts": 0,
                "maps": 0,
                "message": "Renamed HarderFaster · multiview to Lucy.",
            },
        },
    })

    assert fired["drawn"] is True
    assert fired["holdsStored"] is True
    assert fired["nameBeforeAnchor"] is True
    # No `maxlength`, the anchor's rule: it truncates an oversized paste silently.
    assert fired["noMaxlength"] is True

    assert fired["before"] is True
    assert fired["afterEmptying"]["disabled"] is True
    assert "cannot be empty" in fired["afterEmptying"]["count"]
    assert fired["afterTyping"]["disabled"] is False

    assert fired["saved"] == [
        {
            "path": "/api/projects/p1/assets/a1/name",
            "method": "PUT",
            "body": json.dumps({"name": "Lucy"}, separators=(",", ":")),
        }
    ]
    # The reply is a report, so the client has to reach through `project` — adopting the body
    # itself would replace the manifest on screen with a rename report.
    assert fired["adopted"] == "p2"


# ----------------------------------------------------------------------------------------------
# Render timing, surfaced (2026-08-21).
#
# The number is only worth recording if a Director can read it without a Python prompt, and it is
# only *honest* if the caveat travels with it: a `record`-sourced span runs from enqueue, so for
# anything submitted as a batch the queue wait is most of the number. A duration read without
# that caveat is exactly how a 221-frame render came to be recorded as taking 2.2 hours.
#
# So the sentence is written once, in `batch.render_timing_summary`, and the browser's copy is
# *executed under node* against the same jobs and compared character for character. A wording
# that drifts on one side fails here rather than in front of the person reading it.
# ----------------------------------------------------------------------------------------------

#: Every branch of `render_timing_summary`, and every one of them carries a `prompt_id`, a `kind`
#: and a `status` that a real record would have. That was the fixture gap behind two of the
#: defects this table now pins: every non-`complete` row was `record`-sourced, so the branch that
#: described a *ComfyUI-measured* failure as a record span was never executed, and every row
#: carried the empty `prompt_id` that means "local work" without any row actually being local
#: work, so the assembly caveat was never read either.
TIMING_JOBS = [
    # A solo render measured by ComfyUI's own execution clock: a render time, said plainly.
    {"kind": "h3", "prompt_id": "pr1", "status": "complete", "render_seconds": 378.0,
     "render_seconds_source": "comfy", "render_frames": 141, "batch_id": ""},
    # The same length measured off the record, in a batch: the caveat, and the batch named.
    {"kind": "h3", "prompt_id": "pr2", "status": "complete", "render_seconds": 1812.0,
     "render_seconds_source": "record", "render_frames": 226, "batch_id": "batch_1"},
    # Measured off the record but not in a batch: the caveat without the batch claim.
    {"kind": "h3", "prompt_id": "pr3", "status": "complete", "render_seconds": 95.0,
     "render_seconds_source": "record", "render_frames": 0, "batch_id": ""},
    # Never `rendered in`: a cancellation rendered for some unknown part of the time it stood open.
    {"kind": "h3", "prompt_id": "pr4", "status": "cancelled", "render_seconds": 2400.0,
     "render_seconds_source": "record", "render_frames": 141, "batch_id": ""},
    {"kind": "h3", "prompt_id": "pr5", "status": "error", "render_seconds": 3661.0,
     "render_seconds_source": "record", "render_frames": 277, "batch_id": "batch_1"},
    # A render ComfyUI itself timed and that then died: the span is `execution_start` to
    # `execution_error`, so it is time on the GPU and must never be called a record span.
    {"kind": "h3", "prompt_id": "pr6", "status": "error", "render_seconds": 192.0,
     "render_seconds_source": "comfy", "render_frames": 141, "batch_id": ""},
    # A finished export: local work, an empty `prompt_id` by design, never in any queue.
    {"kind": "post", "prompt_id": "", "status": "complete", "render_seconds": 378.0,
     "render_seconds_source": "record", "render_frames": 0, "batch_id": ""},
    # The same export orphaned by a crash and settled at the next boot: the span runs to whenever
    # somebody restarted the application, so this one *is* an upper bound.
    {"kind": "post", "prompt_id": "", "status": "error", "render_seconds": 32400.0,
     "render_seconds_source": "record", "render_frames": 0, "batch_id": ""},
    # A settle whose clock ran backwards: recorded as a settle, with no length claimed.
    {"kind": "h3", "prompt_id": "pr7", "status": "cancelled", "render_seconds": 0.0,
     "render_seconds_source": "unmeasured", "render_frames": 141, "batch_id": ""},
    # Every job written before 2026-08-21: no measurement, and none invented.
    {"kind": "h3", "prompt_id": "pr8", "status": "complete", "render_seconds": 0.0,
     "render_seconds_source": "", "render_frames": 0, "batch_id": ""},
]


def test_the_browsers_timing_sentence_is_the_servers_sentence_character_for_character():
    jobs = [RenderJob(target_id="shot_a", **fields) for fields in TIMING_JOBS]
    wire = json.dumps([json.loads(job.model_dump_json()) for job in jobs])
    body = f"""
      console.log(JSON.stringify({wire}.map(app.renderTimingSummary)));
    """

    assert run_workspace(body) == [render_timing_summary(job) for job in jobs]


def test_the_two_duration_formatters_agree_and_neither_rounds_a_render_up():
    """`6m59s` must never be shown as `7m` on either side. A render is a measurement now."""
    table = [0, 42.9, 59.999, 60, 378, 419.9, 3599, 3600, 7500, -1]
    body = f"""
      console.log(JSON.stringify({json.dumps(table)}.map(app.formatDuration)));
    """

    assert run_workspace(body) == [format_duration(value) for value in table]


def test_the_queue_column_shows_the_caveat_rather_than_hiding_it_in_a_tooltip():
    """`≤` is the caveat made visible: an enqueue-to-settle span is an upper bound on the
    render and never the render itself, and a reader scanning the column has to see that
    without hovering. The full sentence rides the same cell's `title`.

    The two bare `record`-sourced cells are the finished export: local work that was never in a
    queue, so its span is the whole job and a `≤` would claim a wait that cannot have happened.
    The crashed one keeps the mark, because that span runs to the next boot."""
    jobs = [json.loads(RenderJob(**fields).model_dump_json()) for fields in TIMING_JOBS]
    body = f"""
      console.log(JSON.stringify({json.dumps(jobs)}.map(app.renderTimingCell)));
    """

    cells = run_workspace(body)

    assert cells == ["6m18s · 141f", "≤30m12s · 226f", "≤1m35s", "≤40m00s · 141f",
                     "≤1h01m · 277f", "3m12s · 141f", "6m18s", "≤9h00m", "—", "—"]
    # Nothing measured by ComfyUI's own clock is marked as an upper bound -- a failure it timed
    # included, which is the row that used to disagree with the column header above it.
    assert not cells[0].startswith("≤")
    assert not cells[5].startswith("≤")
    # A settle with no length draws no number at all: `0s` would be a claim about a render.
    assert cells[8] == "—"


def test_the_queue_panel_actually_draws_the_column_it_declares():
    """A header with no cell under it, or a cell with no header over it, is a table that lies
    about which column a number is in. Both sides are read, and so is the grid that lays them
    out -- a six-column header over a five-column grid template silently overflows."""
    header = re.search(r'<div class="queue-header">(.*?)</div>', INDEX_HTML.read_text("utf-8"))
    assert header, "the queue table no longer declares a header row"
    columns = re.findall(r"<span[^>]*>([^<]+)</span>", header.group(1))
    assert columns == ["Job", "Target", "Status", "Seed", "Took", "Output"]

    source = without_comments(APP_JS.read_text(encoding="utf-8"))
    row = source.split('list.innerHTML = progress +', 1)[1].split("\n", 1)[0]
    assert 'class="job-took"' in row, "the queue row draws no timing cell under the Took header"
    assert "renderTimingCell(job)" in row
    assert "renderTimingSummary(job)" in row, "the full sentence must ride the cell's title"

    grid = re.search(
        r"\.queue-header, \.job-row \{[^}]*grid-template-columns: ([^;]+);",
        STYLES_CSS.read_text(encoding="utf-8"),
    )
    assert grid, "the queue grid no longer declares its columns"
    assert len(grid.group(1).split()) == len(columns)
