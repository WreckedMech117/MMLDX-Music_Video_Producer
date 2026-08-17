import inspect
import json
import re
import subprocess
from pathlib import Path

from fastapi import HTTPException

from music_video_producer.app import (
    APPLY_DOCUMENTS_LABEL,
    DOCUMENT_LABELS,
    DOCUMENT_LOCK_NOTICE,
    DOCUMENT_RESTORE_REFUSAL,
    SONG_REPLACEMENT_CONSEQUENCE,
    MusicRequest,
    SongPlannerRequest,
    _require_song_replacement_confirmation,
    document_not_requested_notice,
    document_restore_notice,
)
from music_video_producer.models import Project, Shot, Song

APP_JS = Path("src/music_video_producer/web/assets/app.js")
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
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


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
    assert states["balanced"] == {
        "lyricsVisible": True,
        "lyricsRequired": False,
        "durationMin": 4,
        "durationMax": 360,
        "seedMin": 0,
        "seedMax": "18446744073709551615",
    }
    assert states["songplanner-invented"] == {
        "lyricsVisible": False,
        "lyricsRequired": False,
        "durationMin": 30,
        "durationMax": 300,
        "seedMin": 0,
        "seedMax": "4294967295",
    }
    assert states["songplanner-known"] == {
        "lyricsVisible": True,
        "lyricsRequired": True,
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
    assert 'method: "POST"' in call
    # No body, no headers, nothing a message could travel in.
    assert "body:" not in call, call
    assert "JSON.stringify" not in call, call

    template = re.sub(r"\$\{[^}]+\}", "{project_id}", url.group(1))
    assert template in {route.path for route in create_app().routes}, template

    handler = app_js_block("async function expandShotPrompts")
    assert "api.expandShots(projectId)" in handler
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
    assert '$("#expand-shot-prompts").addEventListener("click", expandShotPrompts);' in source

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

    # One spelling, everywhere it is reachable: the markup id, both app.js selectors, the handler.
    assert source.count('$("#expand-shot-prompts")') == 2, source.count('$("#expand-shot-prompts")')
    assert "async function expandShotPrompts()" in source
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
    assert handler.index("shotExpansionInFlight = true;") < handler.index("await shotSaveChain")
    assert "finally { shotExpansionInFlight = false;" in handler
    # Only the expansion raises it, or something else silently blocks every timeline save.
    assert source.count("shotExpansionInFlight = true") == 1, source.count("shotExpansionInFlight = true")

    # The refusal lives in the one function every silent save goes through, ahead of both the
    # queueing and the dirty flags -- a save that is refused was never pending.
    assert "shotExpansionInFlight" in saver
    assert "SHOT_EXPANSION_EDIT_BLOCKED" in saver
    for later in ("state.shotsDirty = true;", "shotSaveChain = shotSaveChain", "api.saveShots("):
        assert saver.index("shotExpansionInFlight") < saver.index(later), later
    # And it is said out loud: the edit really is not saved, and the response re-renders the
    # timeline over it, so a drag that silently vanishes reads as the app losing work at random.
    blocked = run_module("""
      import { SHOT_EXPANSION_EDIT_BLOCKED } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({ blocked: SHOT_EXPANSION_EDIT_BLOCKED }));
    """)["blocked"].lower()
    assert "not saved" in blocked
    assert "again" in blocked, blocked

    # Every timeline mutation goes through that one function rather than calling the route itself.
    assert "api.saveShots(" not in source.replace(saver, ""), "a shot save bypasses saveShotsSilently"


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
    assert "api.expandShots(projectId)" in handler
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
    # And the role reaches the stylesheet at all.
    assert 'class="message ${message.role}"' in APP_JS.read_text(encoding="utf-8")


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
    # Pinned, so a new submitting route cannot appear without this test being read. The four
    # non-Shot routes render a Song, an image or an Asset and have no prompt of a Shot's to check.
    assert set(submitters) == {
        "generate_music",
        "generate_songplanner",
        "generate_flux",
        "generate_multiview",
        "generate_h3",
    }, "a new route submits to ComfyUI; decide whether it is Shot-sourced and guard it"

    shot_sourced = {
        name: source
        for name, (source, signature) in submitters.items()
        if "shot_id" in signature.parameters
    }
    assert shot_sourced, "no route builds a payload from a Shot any more; this test is stale"

    for name, source in shot_sourced.items():
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


def test_the_queue_handler_checks_readiness_once_before_its_loop():
    """A per-Shot refusal discovered mid-loop is a half-submitted batch.

    The route refuses per submission, so without a check ahead of the loop the earlier Shots are
    already queued and burning GPU minutes when the blocked one is reached -- and the plan the
    Director then fixes has takes for half of it already in flight. Position is the guarantee
    here, not presence, so the check, the confirm and the loop are asserted in order.
    """
    body = queue_handler_body()

    assert "api.readiness(projectId)" in body
    # The decision itself is `batchReadinessBlock`, executed by the test below; the handler only
    # asks it and obeys. A filter written out here again is a second rule that can invert alone.
    assert "batchReadinessBlock(report, shots.map((shot) => shot.id))" in body
    for redecided in ("blockedShotIds", "queued.has", "readinessRefusal("):
        assert redecided not in body, redecided

    check = body.index("api.readiness(")
    loop = body.index("for (const shot of shots)")
    confirm = body.index("window.confirm")
    assert check < loop, "readiness is checked inside the loop, so a batch can half-submit"
    # The GPU-cost confirm comes after: nobody is asked to accept a cost for a batch that was
    # never going to be sent.
    assert check < confirm < loop

    # And the refusal returns instead of falling through into the loop.
    refusal = re.search(r"if \(block\.refused\)[^\n]*return;", body)
    assert refusal, body
    assert body.index("block.refused") < confirm


def test_only_a_blocked_shot_inside_the_batch_refuses_it():
    """Negating this filter kept every asserted substring, including `queued.has(id)`.

    Inverted, it lets through exactly the batch that contains a blocked Shot -- producing the
    half-submitted batch the whole check exists to prevent -- while refusing the button over a
    blank draft elsewhere in the plan, which is every plan most of the time. Both directions are
    executed here rather than grepped for, and the refusal is compared to the server's own.
    """
    from music_video_producer.batch import readiness_refusal

    script = """
      import { batchReadinessBlock } from './src/music_video_producer/web/assets/api.js';
      const report = (...ids) => ({ blocking: ids.map((id) => ({ shot_ids: [id], reason: 'x' })) });
      console.log(JSON.stringify({
        insideBatch: batchReadinessBlock(report('shot_b'), ['shot_a', 'shot_b']),
        outsideBatch: batchReadinessBlock(report('shot_c'), ['shot_a', 'shot_b']),
        both: batchReadinessBlock(report('shot_b', 'shot_c'), ['shot_a', 'shot_b']),
        nothingBlocked: batchReadinessBlock({ blocking: [] }, ['shot_a']),
        // The empty-plan note names no Shot, so it can block no batch.
        emptyPlan: batchReadinessBlock({ blocking: [{ shot_ids: [], reason: 'no shots' }] }, ['shot_a']),
        emptyBatch: batchReadinessBlock(report('shot_a'), []),
        noReport: batchReadinessBlock(undefined, ['shot_a']),
        noArguments: batchReadinessBlock(),
        // A real report carries the names the server would have used; the refusal must use them.
        labelled: batchReadinessBlock({ blocking: [
          { shot_ids: ['shot_b'], labels: ['SHOT 04 (shot_b)'], reason: 'x' },
          { shot_ids: ['shot_c'], labels: ['SHOT 09 (shot_c)'], reason: 'x' },
        ] }, ['shot_b']),
      }));
    """

    decisions = run_module(script)
    assert decisions["insideBatch"]["refused"] is True
    assert decisions["insideBatch"]["blocked"] == ["shot_b"]
    # One refusal wording for one rule, the server's.
    assert decisions["insideBatch"]["message"] == readiness_refusal(["shot_b"])
    # A blank draft elsewhere in the plan is not this batch's problem.
    assert decisions["outsideBatch"]["refused"] is False
    assert decisions["outsideBatch"]["blocked"] == []
    assert decisions["outsideBatch"]["message"] == ""
    # And a report carrying both names only the one being submitted.
    assert decisions["both"]["refused"] is True
    assert decisions["both"]["blocked"] == ["shot_b"]
    for allowed in ("nothingBlocked", "emptyPlan", "emptyBatch", "noReport", "noArguments"):
        assert decisions[allowed]["refused"] is False, allowed
    # Named as the server names them: `SHOT 04 (id)`, and only for the Shot in this batch.
    assert decisions["labelled"]["labels"] == ["SHOT 04 (shot_b)"]
    assert decisions["labelled"]["message"] == readiness_refusal(["SHOT 04 (shot_b)"])
    assert "shot_c" not in decisions["labelled"]["message"]
    # A note with no labels still names something rather than "undefined".
    assert decisions["insideBatch"]["labels"] == ["shot_b"]


def test_a_project_switch_during_the_readiness_check_abandons_the_batch():
    """The selector stays live while the readiness GET is in flight.

    Without capturing the id first, the Shot ids collected from project A are submitted against
    whatever project is loaded when the answer lands -- renders queued for a plan nobody asked
    about, against a readiness report that was never checked for it. The expansion handler already
    carries this guard; the batch is the path that spends GPU minutes.
    """
    body = queue_handler_body()

    assert "const projectId = state.project.id;" in body
    assert body.index("const projectId") < body.index("await "), body
    assert "api.readiness(projectId)" in body
    assert "if (state.project?.id !== projectId) return;" in body
    # After the answer, before anything is submitted or accepted as this project's readiness.
    assert body.index("api.readiness(") < body.index("state.project?.id !== projectId")
    assert body.index("state.project?.id !== projectId") < body.index("api.generateH3(")
    assert body.index("state.project?.id !== projectId") < body.index("readinessReport = report")
    # Every submission goes to the captured id, never to whatever is loaded by then.
    assert "api.generateH3(projectId, shot.id)" in body
    assert "api.generateH3(state.project" not in body
    # And the reloads are guarded too: a batch that finished after a switch must not pull the
    # Director back to the project they left.
    assert body.count("await loadProject(projectId)") == 2, body
    for reload in re.findall(r"[^\n]*await loadProject\(projectId\)[^\n]*", body):
        assert "state.project?.id === projectId" in reload, reload


def test_a_batch_that_fails_partway_reports_what_already_queued():
    """The catch toasted the refusal alone, and `loadProject` sat after the loop.

    So a failure on the third of five Shots told the Director "not submitted" while two renders
    were already running, and the queue on screen did not show them either. A Director who reads
    that edits the plan and submits the whole batch again, on top of the half already in flight.
    """
    progress = run_module("""
      import { BATCH_QUEUE_NO_PROGRESS, batchQueueProgress }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        none: batchQueueProgress(0, 5),
        one: batchQueueProgress(1, 5),
        some: batchQueueProgress(3, 5),
        nothingWording: BATCH_QUEUE_NO_PROGRESS,
        absent: batchQueueProgress(),
      }));
    """)

    # Nothing queued is its own sentence: "0 of 5" would still read as a partial batch.
    assert progress["none"] == progress["nothingWording"]
    assert progress["absent"] == progress["nothingWording"]
    assert "no GPU time" in progress["none"]
    for partial, count in (("one", "1"), ("some", "3")):
        assert progress[partial].startswith(f"{count} of 5"), progress[partial]
        # Both halves: what is already running, and what was not sent.
        assert "already rendering" in progress[partial], progress[partial]
        assert "not sent" in progress[partial], progress[partial]

    body = queue_handler_body()
    # Counted as the server accepts them, so the number is what really queued rather than an
    # index the loop had reached.
    assert "let queued = 0;" in body
    assert "for (const shot of shots) { await api.generateH3(projectId, shot.id); queued += 1; }" in body
    assert "batchQueueProgress(queued, shots.length)" in body
    # Reported in the failure toast itself, not as a second toast that can be missed.
    catch = body.split("} catch (error) {", 1)[1]
    assert "batchQueueProgress(" in catch
    assert catch.index("toast(") < catch.index("loadProject("), catch
    # And what did queue is shown, or the queue table contradicts the toast beside it.
    assert "if (queued && state.project?.id === projectId) await loadProject(projectId);" in catch


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

    assert "const cell = shotPromptCell(shot);" in body
    for drawn in ("cell.className", "escapeHtml(cell.text)"):
        assert drawn in body, drawn
    # Both the tooltip and the accessible name, from the one label.
    assert 'title="${escapeHtml(cell.label)}"' in body
    assert 'aria-label="${escapeHtml(cell.label)}"' in body
    # No second copy of the decision, in any of its spellings.
    for redecided in ("promptIsMissing", "SHOT_WITHOUT_PROMPT_FLAG", "Untitled shot"):
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
    from music_video_producer.batch import (
        SHOT_WITHOUT_PROMPT,
        SHOTS_SHARE_ONE_PROMPT,
        readiness_refusal,
    )

    report = server_readiness_report(tmp_path)
    script = f"""
      import {{ batchReadinessBlock, blockedShotIds, blockedShotLabels, queueButtonState,
        readinessLines, readinessSummary }}
        from './src/music_video_producer/web/assets/api.js';
      const report = {json.dumps(report)};
      console.log(JSON.stringify({{
        blocked: blockedShotIds(report),
        labels: blockedShotLabels(report),
        lines: readinessLines(report),
        summary: readinessSummary(report),
        insideBatch: batchReadinessBlock(report, ['shot_blank', 'shot_written']),
        outsideBatch: batchReadinessBlock(report, ['shot_written', 'shot_echo']),
        button: queueButtonState(report, [{{ id: 'shot_written' }}, {{ id: 'shot_blank' }}]),
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
    assert parsed["insideBatch"]["refused"] is True
    assert parsed["insideBatch"]["message"] == readiness_refusal(blocked_labels)
    assert parsed["outsideBatch"]["refused"] is False
    assert parsed["button"]["disabled"] is True
    assert parsed["button"]["title"] == readiness_refusal(blocked_labels)

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
      import {{ readinessLines, readinessSummary, queueButtonState }}
        from './src/music_video_producer/web/assets/api.js';
      const report = {json.dumps(report)};
      console.log(JSON.stringify({{
        lines: readinessLines(report),
        summary: readinessSummary(report),
        button: queueButtonState(report, []),
      }}));
    """)

    assert [line["shotIds"] for line in parsed["lines"]] == [[]]
    assert parsed["lines"][0]["text"] == f"Blocked - {PLAN_WITHOUT_SHOTS}"
    assert parsed["summary"].startswith("0 of 0 shots have a prompt")
    assert parsed["button"]["disabled"] is True


def test_the_batch_button_says_no_before_the_click_rather_than_after_it():
    """`#queue-ready` was enabled purely from the ready-status count.

    So a batch the route would certainly refuse looked fully submittable, and the Director learned
    otherwise by spending the click. Readiness is a cheap GET, fetched on project load, and the
    button's whole state is decided from it here -- including the case that must *not* disable it,
    a blocked draft elsewhere in the plan.
    """
    script = """
      import { QUEUE_WITHOUT_READY_SHOTS, queueButtonState }
        from './src/music_video_producer/web/assets/api.js';
      const blocking = (...ids) => ({ blocking: ids.map((id) => ({ shot_ids: [id], reason: 'x' })) });
      const shot = (id) => ({ id, status: 'ready' });
      console.log(JSON.stringify({
        emptyWording: QUEUE_WITHOUT_READY_SHOTS,
        nothingReady: queueButtonState(blocking(), []),
        allWritten: queueButtonState(blocking(), [shot('shot_a'), shot('shot_b')]),
        one: queueButtonState(blocking(), [shot('shot_a')]),
        blockedInside: queueButtonState(blocking('shot_b'), [shot('shot_a'), shot('shot_b')]),
        blockedElsewhere: queueButtonState(blocking('shot_c'), [shot('shot_a'), shot('shot_b')]),
        // Nothing fetched yet must not disable a batch the server has not been asked about.
        noReport: queueButtonState(null, [shot('shot_a')]),
      }));
    """

    states = run_module(script)
    assert states["nothingReady"]["disabled"] is True
    assert states["nothingReady"]["title"] == states["emptyWording"]
    assert states["allWritten"] == {"disabled": False, "blocked": [], "title": "Queue 2 reviewed H3 shots"}
    assert states["one"]["title"] == "Queue 1 reviewed H3 shot"
    # The refusal is stated before the click, in the words it would be refused with.
    assert states["blockedInside"]["disabled"] is True
    assert states["blockedInside"]["blocked"] == ["shot_b"]
    assert "Write a prompt in the shot inspector" in states["blockedInside"]["title"]
    # And a blank draft elsewhere leaves the batch submittable.
    assert states["blockedElsewhere"]["disabled"] is False
    assert states["noReport"]["disabled"] is False

    source = APP_JS.read_text(encoding="utf-8")
    jobs = without_comments(app_js_block("function renderJobs"))
    assert "const queue = queueButtonState(readinessReport, queueable);" in jobs
    assert '$("#queue-ready").disabled = queue.disabled;' in jobs
    assert '$("#queue-ready").title = queue.title;' in jobs
    # No second rule for the same button: the count alone is what could not see a refusal coming.
    assert "queueable.length === 0" not in jobs, jobs

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
