import inspect
import json
import re
import subprocess
from pathlib import Path

from fastapi import HTTPException

from music_video_producer.app import (
    DOCUMENT_LABELS,
    DOCUMENT_RESTORE_REFUSAL,
    SONG_REPLACEMENT_CONSEQUENCE,
    MusicRequest,
    SongPlannerRequest,
    _require_song_replacement_confirmation,
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

    handler = APP_JS.read_text(encoding="utf-8").split(
        '$("#chat-form").addEventListener("submit"', 1
    )[1].split("  });", 1)[0]
    assert "const before = state.project;" in handler
    assert "documentChangeToast(before, state.project)" in handler
    # The old unconditional claim is gone from the whole module, in any handler.
    assert "Treatment updated" not in APP_JS.read_text(encoding="utf-8")


def test_editor_overwrites_warn_before_discarding_unsaved_edits_and_clear_the_flags():
    """Unsaved textarea edits are the one loss this feature cannot undo.

    A restore and a Director reply both re-render the editors from the server, and the captured
    "previous version" is the *stored* text -- so on-screen edits are unrecoverable. Both paths
    ask first, matching the `window.confirm` precedent on project switch, and both then clear
    the dirty flags exactly as `saveProject` does: text that matches the server is not dirty,
    and a project permanently flagged dirty teaches the Director to click through the one
    question that protects real work.
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
