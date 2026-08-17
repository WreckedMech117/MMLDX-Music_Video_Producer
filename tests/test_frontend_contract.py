import json
import re
import subprocess
from pathlib import Path

APP_JS = Path("src/music_video_producer/web/assets/app.js")
INDEX_HTML = Path("src/music_video_producer/web/index.html")

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


def run_module(script: str):
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


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
    assert states["balanced"] == {"lyricsVisible": True, "lyricsRequired": False, "durationMax": 360}
    assert states["songplanner-invented"] == {
        "lyricsVisible": False,
        "lyricsRequired": False,
        "durationMax": 200,
    }
    assert states["songplanner-known"] == {
        "lyricsVisible": True,
        "lyricsRequired": True,
        "durationMax": 200,
    }


def test_sync_music_variant_consumes_the_shared_preset_field_state():
    handler = APP_JS.read_text(encoding="utf-8").split("const syncMusicVariant", 1)[1].split("};", 1)[0]

    assert "musicPresetFieldState(musicForm.elements.preset.value)" in handler
    assert "fields.lyricsVisible" in handler
    assert "fields.lyricsRequired" in handler
    assert "fields.durationMax" in handler
    # The three-way preset logic must not be re-derived from raw preset strings here.
    assert "songplanner-known" not in handler
    assert "songplanner-invented" not in handler


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
