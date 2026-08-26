from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

# The render length and the take's own seconds of the song, imported rather than re-derived.
# `build_h3_reference_payload` computes the 17k+5 rounding inline and `tests/test_workflows.py`
# pins the two against each other over the whole window range; the audio-replace pair is new
# code with no such history to preserve, so it calls the functions — and calling them is the
# whole of what makes the restored audio match the take (2026-08-21). `timeline` imports only
# `models`, which imports nothing local, so this direction cannot cycle.
from .timeline import over_render_frames, over_render_window


@dataclass(frozen=True, slots=True)
class WorkflowEntry:
    id: str
    name: str
    category: str
    relative_path: str
    description: str
    available: bool


class WorkflowCatalog:
    DEFINITIONS = (
        ("flux-image-gen", "Flux Image Generation", "asset", "Flux-Image-Gen.json", "Character and setting concept images"),
        ("music3-balanced", "MiniMax Music 3 Balanced", "song", r"Audio\MiniMax Music 3\SongPlanner + MiniMax Music 3 - Balanced Official.json", "SongPlanner and Music 3 production workflow"),
        ("krea-multiview", "Krea 2 Multiview", "asset", r"Video\Music Video Advanced\02 - Krea 2 Character Sheet.json", "Character sheet and recurring-character views"),
        ("h3-director", "MiniMax H3 Director", "video", r"Video\MiniMax H3 Ultra & Director\01 - MiniMax H3 Ultra V2.json", "Director timeline and H3 shot generation"),
        ("ltx25-enhance", "LTX 2.5 Enhancement", "video", r"Video\Music Video Advanced\04 - H3 Music Video - LTX 2.5 READY.json", "Video enhancement and finishing"),
    )

    def __init__(self, workflow_root: Path):
        self.workflow_root = Path(workflow_root)

    def list(self) -> list[WorkflowEntry]:
        return [
            WorkflowEntry(
                id=identifier,
                name=name,
                category=category,
                relative_path=relative,
                description=description,
                available=(self.workflow_root / Path(relative)).exists(),
            )
            for identifier, name, category, relative, description in self.DEFINITIONS
        ]


def build_flux_payload(
    *,
    prompt: str,
    width: int,
    height: int,
    steps: int,
    guidance: float,
    seed: int,
    prefix: str,
) -> dict[str, dict[str, Any]]:
    return {
        "3": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux1-dev-fp8.safetensors", "weight_dtype": "fp8_e4m3fn"}},
        "4": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": "ViT-L-14-TEXT-detail-improved-hiT-GmP-TE-only-HF.safetensors", "clip_name2": "t5xxl_fp16.safetensors", "type": "flux", "device": "default"}},
        "6": {"class_type": "ModelSamplingFlux", "inputs": {"model": ["3", 0], "max_shift": 1.15, "base_shift": 0.5, "width": width, "height": height}},
        "8": {"class_type": "BasicGuider", "inputs": {"model": ["6", 0], "conditioning": ["10", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["15", 0], "vae": ["13", 0]}},
        "10": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["11", 0], "guidance": guidance}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 0]}},
        "12": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": prefix}},
        "13": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "14": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "15": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["19", 0], "guider": ["8", 0], "sampler": ["14", 0], "sigmas": ["16", 0], "latent_image": ["20", 0]}},
        "16": {"class_type": "BasicScheduler", "inputs": {"model": ["6", 0], "scheduler": "simple", "steps": steps, "denoise": 1.0}},
        "19": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "20": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
    }


def build_music3_payload(
    *, caption: str, lyrics: str, duration: float, seed: int, prefix: str
) -> dict[str, dict[str, Any]]:
    return {
        "44": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_music3_dit_fp16.safetensors", "weight_dtype": "default"}},
        "45": {"class_type": "MiniMaxMusic3TextEncode", "inputs": {"clip": ["46", 0], "caption": caption, "lyrics": lyrics, "seed": seed, "max_duration": duration, "cfg_scale": 1.7, "top_k": 50}},
        "46": {"class_type": "CLIPLoader", "inputs": {"clip_name": "minimax_music3_text_encoder_pruned_int8_convrot.safetensors", "type": "minimax", "device": "default"}},
        "47": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_music3_dav.safetensors"}},
        "48": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["45", 0]}},
        "49": {"class_type": "EmptyMiniMaxMusic3LatentAudio", "inputs": {"seconds": duration, "batch_size": 1}},
        "50": {"class_type": "KSampler", "inputs": {"model": ["44", 0], "seed": seed, "steps": 30, "cfg": 1.7, "sampler_name": "euler", "scheduler": "simple", "positive": ["45", 0], "negative": ["48", 0], "latent_image": ["49", 0], "denoise": 1.0}},
        "51": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["50", 0], "vae": ["47", 0]}},
        "35": {"class_type": "SaveAudioAdvanced", "inputs": {"audio": ["51", 0], "filename_prefix": prefix, "format": "flac"}},
    }


#: What ``MiniMaxMusic3TextEncode.max_duration`` accepts, read off the recorded
#: ``/object_info`` (0.04–360 s). Only the ceiling is named here: every SongPlanner request
#: is already above the floor by the planner's own 30 s minimum, and it is the *product* of
#: the requested duration and the headroom below that can leave the schema. Past it ComfyUI
#: rejects the whole prompt at ``/prompt`` validation, which reaches the Director as an
#: opaque 502 — so the adapter refuses locally and names the numbers instead.
MUSIC3_MAX_DURATION_SECONDS = 360.0

#: ``M3SongPlanner.duration_seconds`` and ``MiniMaxMusic3TextEncode.max_duration`` take the
#: same kind of number and mean different things. The planner is told **how long a song to
#: write**; the encoder is given a **latent ceiling** the song may finish before. Handing the
#: target to both leaves no room, so a song whose lyrics run slightly long loses its ending.
#: The multiplier between the two is the headroom.
#:
#: 1.5 is the creator's documented rule — "max_duration: set this 50% longer than your target
#: (60s lyrics → 90s max duration)" — stated four separate times in their write-up, once as a
#: first-hand account of an ending being cut off. Their own audited export sets both inputs to
#: 200, equal, no headroom at all. The evidence disagrees with itself, which is why the
#: headroom is a request field the Director can see and change rather than a constant buried
#: here: only a long, lyric-dense song settles it by ear, and both live SongPlanner runs so
#: far sat at the 30 s floor and returned 29.989 s, precisely where this could never show.
SONGPLANNER_DEFAULT_DURATION_HEADROOM = 1.5

#: The widest headroom any acceptable request could survive. ``SongPlannerRequest.duration``
#: cannot go below the planner's 30 s floor, and 30 × 12 is exactly
#: ``MUSIC3_MAX_DURATION_SECONDS`` — so above 12 there is no duration at all the route would
#: accept, and the refusal should name the setting rather than a product that was never
#: reachable. ``tests/test_workflows.py`` re-derives this from the recorded schema.
SONGPLANNER_MAX_DURATION_HEADROOM = 12.0


def _build_songplanner_core(
    *,
    idea: str,
    genre_hint: str,
    duration: float,
    duration_headroom: float,
    seed: int,
    prefix: str,
    lyrics: str | None,
) -> dict[str, dict[str, Any]]:
    """Shared SongPlanner → Music 3 core.

    Adapted from the audited ``songplanner-invented-user-export.json``: UI-only
    preview nodes (57/58/59), the CR Text scratchpad (63), the SeedNode
    indirection (52), and the dead tiled-decode branch (53/54) are dropped; the
    literal seed feeds nodes 45 and 50 directly, and the master is saved as FLAC
    instead of the export's mp3/V0. ``lyrics=None`` keeps the invented path where
    Gemma-3 writes both caption and lyrics; a non-empty string (the known-lyrics
    path) replaces the planner's lyrics with a known lyric sheet passed literally
    to node 45, matching ``build_music3_payload``.

    ``lyrics`` is deliberately not defaulted: both wrappers must state which
    variant they want, so no caller can silently fall into the invented path
    while asking for a cover. ``duration_headroom`` is not defaulted for the same
    reason — the two duration inputs mean different things (see
    ``SONGPLANNER_DEFAULT_DURATION_HEADROOM``), and a caller that has not said how
    much room the song gets to overrun into should have to say so rather than
    silently re-conflate them. ``duration`` reaches ``M3SongPlanner`` untouched
    whatever the headroom is: a song is asked for at the length that was asked for.
    """
    if lyrics is not None and not lyrics.strip():
        raise ValueError("Known lyrics must not be empty; pass None for invented lyrics")
    if duration_headroom < 1.0:
        raise ValueError(
            f"A duration headroom of {duration_headroom:g} puts "
            f"MiniMaxMusic3TextEncode.max_duration below the song asked for, which can only "
            f"truncate it; 1.0 gives the encoder exactly the target and no room to overrun"
        )
    # Exactly 1.0 hands the encoder the target *object*, not ``duration * 1.0``: multiplying
    # would turn an integer 120 into 120.0 and change the JSON that goes out. A headroom of
    # 1.0 has to reproduce the pre-headroom payload byte for byte — that is the evidence this
    # change is inert where it is meant to be.
    max_duration = duration if duration_headroom == 1.0 else duration * duration_headroom
    if max_duration > MUSIC3_MAX_DURATION_SECONDS:
        raise ValueError(
            f"A {duration:g}s song at {duration_headroom:g}x headroom sets "
            f"MiniMaxMusic3TextEncode.max_duration={max_duration:g}, above that node's "
            f"maximum of {MUSIC3_MAX_DURATION_SECONDS:g}; lower the headroom or the duration"
        )
    payload: dict[str, dict[str, Any]] = {
        "55": {"class_type": "M3SongPlanner", "inputs": {"text_encoder": "gemma_3_12B_it_fp4_mixed.safetensors", "idea": idea, "genre_hint": genre_hint, "vocal_config": "female vocals", "language": "English", "duration_seconds": duration, "seed": seed, "temperature": 0.8, "top_p": 0.95, "top_k": 64, "max_tokens": 2048, "keep_model_loaded": False}},
        "44": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_music3_dit_fp16.safetensors", "weight_dtype": "default"}},
        "45": {"class_type": "MiniMaxMusic3TextEncode", "inputs": {"clip": ["46", 0], "caption": ["55", 0], "lyrics": ["55", 1], "seed": seed, "max_duration": max_duration, "cfg_scale": 1.5, "top_k": 50}},
        "46": {"class_type": "CLIPLoader", "inputs": {"clip_name": "minimax_music3_text_encoder_bf16.safetensors", "type": "minimax", "device": "default"}},
        "47": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_music3_dav.safetensors"}},
        "48": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["45", 0]}},
        "49": {"class_type": "EmptyMiniMaxMusic3LatentAudio", "inputs": {"seconds": ["45", 1], "batch_size": 1}},
        "50": {"class_type": "KSampler", "inputs": {"model": ["44", 0], "seed": seed, "steps": 30, "cfg": 1.7, "sampler_name": "euler", "scheduler": "simple", "positive": ["45", 0], "negative": ["48", 0], "latent_image": ["49", 0], "denoise": 1.0}},
        "51": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["50", 0], "vae": ["47", 0]}},
        "35": {"class_type": "SaveAudioAdvanced", "inputs": {"audio": ["51", 0], "filename_prefix": prefix, "format": "flac"}},
    }
    if lyrics is not None:
        payload["45"]["inputs"]["lyrics"] = lyrics
    return payload


def build_songplanner_invented_payload(
    *, idea: str, genre_hint: str, duration: float, duration_headroom: float, seed: int, prefix: str
) -> dict[str, dict[str, Any]]:
    """SongPlanner invented-lyrics path: Gemma-3 writes caption + lyrics in-graph."""
    return _build_songplanner_core(
        idea=idea,
        genre_hint=genre_hint,
        duration=duration,
        duration_headroom=duration_headroom,
        seed=seed,
        prefix=prefix,
        lyrics=None,
    )


def build_songplanner_known_lyrics_payload(
    *,
    idea: str,
    genre_hint: str,
    lyrics: str,
    duration: float,
    duration_headroom: float,
    seed: int,
    prefix: str,
) -> dict[str, dict[str, Any]]:
    """SongPlanner known-lyrics path: the supplied lyric sheet reaches node 45 unchanged.

    Matches the audited ``songplanner-known-lyrics-user-export.json``, which differs
    from the invented export only in feeding node 45's ``lyrics`` from a lyric-sheet
    text node instead of the planner; the adapter passes the sheet as a literal and
    never rewrites, reflows, or truncates it. Callers that accept Director input
    (the ``/generate/songplanner`` route) strip leading/trailing whitespace before
    calling — edge whitespace is not lyric content — but every interior character,
    blank line, and indent survives byte for byte.

    ``lyrics`` must be a non-empty string: this function only builds the cover
    variant, so ``None`` is rejected rather than degrading to invented lyrics.
    """
    if not isinstance(lyrics, str):
        raise TypeError(
            "Known lyrics must be a string; use build_songplanner_invented_payload "
            "for the invented-lyrics path"
        )
    if not lyrics.strip():
        raise ValueError(
            "Known lyrics must not be empty; use build_songplanner_invented_payload "
            "for the invented-lyrics path"
        )
    return _build_songplanner_core(
        idea=idea,
        genre_hint=genre_hint,
        duration=duration,
        duration_headroom=duration_headroom,
        seed=seed,
        prefix=prefix,
        lyrics=lyrics,
    )


#: Frames per second every H3 window is measured in. One constant so the adapter's
#: seconds-to-frames conversion and the tests that check it cannot drift apart.
H3_FRAME_RATE = 24

#: The ceilings ``MiniMaxH3DirectorCS`` declares: ``duration_frames``/``end_frame`` cap at
#: 10000 and ``start_second``/``end_second`` at 1000.0. Above either, ComfyUI rejects the
#: whole prompt at ``/prompt`` validation and the Director sees an opaque 502 after the
#: submission round-trip. The text-only path is the one with live render evidence, so it
#: gets the same local refusal the reference path has.
H3_DIRECTOR_MAX_FRAMES = 10000
H3_DIRECTOR_MAX_SECONDS = 1000.0

#: The step count the text-only Director path uses when the request names none. This was
#: ``H3Request.steps``'s own default until the reference path grew sampling profiles and
#: that default had to become "unset" so an omitted count could fall through to the
#: profile's. The number is unchanged, and this path is deliberately *not* offered a turbo
#: profile: it loads a different checkpoint pair through ``MiniMaxH3DirectorCS``, the
#: installed generic H3 turbo LoRAs are not the ``ref2v`` one, and nothing has been
#: rendered live that way.
H3_DIRECTOR_DEFAULT_STEPS = 20

#: The frame the text-only Director path uses when the request names none. This was
#: ``H3Request.width``/``height``'s own default until the reference path grew a
#: ``ResolutionSelector`` and those fields had to become "unset" so an omitted size could fall
#: through to the selection. The numbers are unchanged, and this path is deliberately *not*
#: offered the selector: ``MiniMaxH3DirectorCS`` is a different node with its own
#: ``divisible_by``/``resize_method`` pair, node ``115`` of ``h3-ltx25-user-export.json`` sits
#: in the reference chain rather than this one, and no frame from this graph has been measured
#: at 0.6 MP. Moving it would be a size change nobody evidenced, on the one H3 path that is
#: currently correct.
H3_DIRECTOR_DEFAULT_WIDTH = 1344
H3_DIRECTOR_DEFAULT_HEIGHT = 768


def _finite(name: str, value: float) -> float:
    """A window value ComfyUI could act on. ``inf`` and ``nan`` are neither.

    Without this, ``inf`` reaches the frame arithmetic and raises ``OverflowError`` —
    not a ``ValueError`` — so the route's refusal translation misses it and the
    Director gets a 500 for a number the request model happily accepted.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number of seconds, not {value!r}")
    return float(value)


def build_h3_director_payload(
    *,
    timeline_data: str,
    duration: float,
    requested_frames: int,  # must already sit on H3's 17k+5 grid; see timeline.align_h3_frames
    seed: int,
    width: int,
    height: int,
    prefix: str,
    steps: int | None = None,
    start: float = 0,
) -> dict[str, dict[str, Any]]:
    """Build the selected text-only H3 Director path with explicit loaders.

    ``steps`` left as ``None`` takes ``H3_DIRECTOR_DEFAULT_STEPS``, which is the number
    ``H3Request`` defaulted to before the reference path grew sampling profiles. This
    path is offered no profile: see that constant.
    """
    steps = H3_DIRECTOR_DEFAULT_STEPS if steps is None else steps
    _finite("Shot duration", duration)
    _finite("Shot start", start)
    # Every window literal this payload sends, against the maximum its node declares for it.
    # Written as data rather than as one condition because the node caps the frame counts and
    # the seconds independently: at 24 fps the frame cap binds first, and this stays correct
    # if that ever stops being true. `tests/preflight_h3_ultra.py` checks each maximum here
    # against the live schema.
    for name, value, maximum in (
        ("duration_frames", requested_frames, H3_DIRECTOR_MAX_FRAMES),
        ("start_frame", round(start * H3_FRAME_RATE), H3_DIRECTOR_MAX_FRAMES),
        ("end_frame", round((start + duration) * H3_FRAME_RATE), H3_DIRECTOR_MAX_FRAMES),
        ("start_second", start, H3_DIRECTOR_MAX_SECONDS),
        ("end_second", start + duration, H3_DIRECTOR_MAX_SECONDS),
    ):
        if value > maximum:
            raise ValueError(
                f"A shot from {start:g}s to {start + duration:g}s sends {name}={value:g}, "
                f"above the H3 Director node's maximum of {maximum:g}"
            )
    timeline = json.loads(timeline_data)
    segments = timeline.get("segments", [])
    local_prompts = "\n\n".join(str(item.get("prompt", "")) for item in segments)
    segment_lengths = ",".join(str(item.get("length", 0)) for item in segments)
    timeline.update(
        {
            "mainTrackEnabled": True,
            "audioTrackEnabled": False,
            "motionTrackEnabled": False,
            "overrideAudio": False,
            "inpaint_audio": True,
            "global_prompt": "",
            "reference_mode": "OFF",
            "prompt_format": "minimax",
            "normalStartFrame": 0,
            "normalDurationFrames": requested_frames,
            "subjects": [],
            "motionSegments": [],
            "audioSegments": [],
        }
    )
    end = start + duration
    return {
        "mvp:model": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "mvp:model_ref": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "mvp:clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "type": "minimax", "device": "default"}},
        "mvp:video_vae": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "mvp:audio_vae": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "2343": {
            "class_type": "MiniMaxH3DirectorCS",
            "inputs": {
                "clip": ["mvp:clip", 0], "vae": ["mvp:video_vae", 0], "audio_vae": ["mvp:audio_vae", 0],
                "model": ["mvp:model", 0], "model_ref2va": ["mvp:model_ref", 0],
                "start_second": start, "end_second": end, "duration_seconds": duration,
                "start_frame": round(start * H3_FRAME_RATE), "end_frame": round(end * H3_FRAME_RATE), "duration_frames": requested_frames,
                "timeline_data": json.dumps(timeline, separators=(",", ":")),
                "local_prompts": local_prompts, "segment_lengths": segment_lengths, "guide_strength": "",
                "use_custom_audio": False, "use_custom_motion": False, "inpaint_audio": True,
                "frame_rate": H3_FRAME_RATE, "display_mode": "seconds", "custom_width": width, "custom_height": height,
                "resize_method": "crop", "divisible_by": 32, "img_compression": 0, "override_audio": False,
                "ref_image_size": "match", "shift_video": 12, "shift_audio": 4,
                "ref_image_notes": "",
            },
        },
        "2344": {"class_type": "ModelPreviewOverrideKJ", "inputs": {"max_resolution": 1024, "jpeg_quality": 80, "suppress_default_preview": True, "preview_frames": 12, "preview_fps": 12, "model": ["2343", 0]}},
        "2345": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "2346": {"class_type": "BasicScheduler", "inputs": {"scheduler": "simple", "steps": steps, "denoise": 1.0, "model": ["2344", 0]}},
        "2347": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "2351:2170": {"class_type": "BasicGuider", "inputs": {"model": ["2344", 0], "conditioning": ["2343", 1]}},
        "2351:2173": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["2347", 0], "guider": ["2351:2170", 0], "sampler": ["2345", 0], "sigmas": ["2346", 0], "latent_image": ["2343", 2]}},
        "2351:2174": {"class_type": "VAEDecode", "inputs": {"samples": ["2351:2173", 0], "vae": ["mvp:video_vae", 0]}},
        "2351:2175": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["2351:2173", 0], "vae": ["mvp:audio_vae", 0]}},
        "2348": {"class_type": "VHS_VideoCombine", "inputs": {"frame_rate": 24, "loop_count": 0, "filename_prefix": prefix, "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 19, "save_metadata": True, "trim_to_audio": False, "pingpong": False, "save_output": True, "images": ["2351:2174", 0], "audio": ["2351:2175", 0]}},
    }


#: References the H3 Ultra graph accepts per kind, and the numbers the refusal quotes.
#:
#: These are not this adapter's policy: ``MiniMaxH3ReferenceToVideo`` declares them as the
#: ``max`` of its ``ref_images`` / ``ref_videos`` / ``ref_audios`` autogrow groups, so a tenth
#: picture has no slot to land in. Named here so ``tests/preflight_h3_ultra.py`` can read the
#: adapter's numbers and compare them against the live schema's, rather than restating them.
H3_REFERENCE_LIMITS = {"picture": 9, "video": 3, "audio": 3}

#: Where each kind's slots start in ``MiniMaxH3ReferenceSplitter``'s flat output list, which the
#: live schema names ``picture_1…9``, ``video_1…3``, ``video_audio_1…3``, ``audio_1…3`` — one
#: output per slot the limits above allow, in that order. The audit checks these against the
#: live ``output_name`` list; wiring a reference to the wrong index would feed the conditioner
#: someone else's media without failing validation.
H3_SPLIT_OFFSETS = {
    "picture": 0,
    "video": H3_REFERENCE_LIMITS["picture"],
    "video_audio": H3_REFERENCE_LIMITS["picture"] + H3_REFERENCE_LIMITS["video"],
    "audio": H3_REFERENCE_LIMITS["picture"] + 2 * H3_REFERENCE_LIMITS["video"],
}

#: The conditioner input each split group is wired into, by the same four names
#: ``H3_SPLIT_OFFSETS`` keys. Beside the offsets rather than inline in the builder so that the
#: group a reference lands in, the output index it is read from and the input it is written to
#: are all decided from one value — ``H3ReferenceSlot.group`` — instead of from three parallel
#: branches that can each be edited without the others.
H3_SPLIT_INPUTS = {
    "picture": "ref_images.ref_image_{slot}",
    "video": "ref_videos.ref_video_{slot}",
    "video_audio": "ref_video_audios.ref_video_audio_{slot}",
    "audio": "ref_audios.ref_audio_{slot}",
}


@dataclass(frozen=True, slots=True)
class H3ReferenceSlot:
    """One media slot the loader will fill, and which reference fills it.

    ``index`` is the position in the caller's ``references`` list, ``group`` one of
    ``H3_SPLIT_OFFSETS``' four keys, and ``slot`` the 0-based position *within that group* —
    which is the only number the wiring may use, because the splitter's outputs are per-group.
    """

    index: int
    group: str
    slot: int


def partition_h3_references(references: list[dict[str, Any]]) -> tuple[H3ReferenceSlot, ...]:
    """Which media slot each reference lands in, modelled on the loader node's own grouping.

    **This is a mirror, not an import.** ``MiniMaxH3MediaLoader`` lives in the GPL extension
    ``ComfyUI-Fantastic-MiniMaxH3-PromptBuilder`` (``nodes.py``, the ``_partition`` static method
    on that class — *not* on ``MiniMaxH3ReferenceSplitter``, which only pads each group to its
    fixed width and concatenates them). It is outside this project and not a dependency, so its
    behaviour is modelled here from reading it and its source is not copied. Everything below is
    a statement about what that node does with a ``media_state`` list; ``tests/test_workflows.py``
    states the same rules as expectations, so a drift between the two sides fails offline instead
    of rendering wrongly.

    The rules, as the node applies them, in the order it applies them:

    * **A reference switched off consumes no slot.** The node skips any item whose ``enabled`` is
      exactly ``False`` — identity, not truthiness, so ``0`` and ``null`` are *not* off — and the
      numbering closes up around it. This is why ``enabled`` cannot be ignored on this side: the
      builder deliberately serialises the flag into ``media_state``, so the node acts on it.
    * **A picture joins the picture group**, in list order.
    * **A video always joins the video group**, and *always* adds an entry to the paired-audio
      group, which is therefore positional against the video group: video *N*'s soundtrack is
      paired-audio *N* and nothing else. The entry is the video's own track when the item both
      has audio and is ``paired`` (the node's default reading when no ``audio_mode`` is given),
      and empty otherwise — so a silent video, or one whose audio was routed elsewhere, still
      holds its place. An empty entry yields no slot here, because there is nothing to wire.
    * **A video whose audio is ``standalone`` also joins the audio group**, at the point the
      video appears in the list. It therefore *displaces* every ``audio`` reference after it —
      the third consequence this function exists to keep both sides agreeing about.
    * **An audio joins the audio group**, in list order.

    Only values the node branches on matter: it tests ``audio_mode`` against ``"paired"`` and
    ``"standalone"`` and nothing else, so any other spelling falls through both tests and leaves
    the soundtrack unsent. A test passing an unrecognised value proves nothing about either rule.

    Returned in **reference order**, so the caller emits its wiring in the order the Director
    attached the media; a standalone video yields two slots, its video and its audio, in that
    order. An unwirable ``kind`` is refused here rather than dropped, for *every* item including
    a disabled one — the node would ignore it silently, and a caller that sent one has a defect
    the payload cannot express. That is the single deliberate divergence, and it only ever
    refuses; it never wires anything the node would wire differently.
    """
    slots: list[H3ReferenceSlot] = []
    filled = dict.fromkeys(H3_SPLIT_INPUTS, 0)
    for index, item in enumerate(references):
        kind = item.get("kind")
        if kind not in ("picture", "video", "audio"):
            raise ValueError(f"Unsupported H3 reference kind: {kind}")
        if item.get("enabled") is False:
            continue
        if kind == "video":
            slots.append(H3ReferenceSlot(index, "video", filled["video"]))
            mode = item.get("audio_mode", "paired")
            carries_audio = bool(item.get("has_audio"))
            if carries_audio and mode == "paired":
                # Positional against the video group, so the slot number is the video's own.
                slots.append(H3ReferenceSlot(index, "video_audio", filled["video"]))
            filled["video"] += 1
            if not (carries_audio and mode == "standalone"):
                continue
            kind = "audio"
        slots.append(H3ReferenceSlot(index, kind, filled[kind]))
        filled[kind] += 1
    return tuple(slots)


def h3_reference_group_counts(
    references: list[dict[str, Any]], slots: tuple[H3ReferenceSlot, ...]
) -> dict[str, int]:
    """How many of each kind ``H3_REFERENCE_LIMITS`` bounds a payload against. **The larger of two.**

    The loader node counts its own media *twice*, in two places, and the two disagree — which is
    why this cannot be one number. Both have to be satisfied or a payload passes here and is
    refused after submission, which reaches the Director as an opaque 502.

    * Its ``VALIDATE_INPUTS`` counts every item of a kind in ``media_state``, **including the
      switched-off ones**, and refuses more than nine pictures or three videos before the graph
      runs at all. So a tenth picture is a tenth picture even when it is off.
    * Its ``_partition`` skips the switched-off ones and then *truncates* each group to the same
      ceilings, dropping the overflow without a word. That is the count the wiring has to fit
      inside, and it is not bounded by the attached count: a ``standalone`` video's soundtrack
      joins the audio group, so three attached audios plus one such video is four audios — and
      the fourth would be wired to a splitter output that does not exist. Nothing checks audios
      in ``VALIDATE_INPUTS`` at all, so this is the only count that catches it.

    Taking the larger satisfies both, and each kind's number is then the one the Director is
    told: whichever count overflowed is the count quoted. ``video_audio`` appears in neither,
    because it can never exceed the video group it is positional against.
    """
    attached = {
        kind: sum(1 for item in references if item.get("kind") == kind)
        for kind in H3_REFERENCE_LIMITS
    }
    wired = {
        kind: sum(1 for entry in slots if entry.group == kind) for kind in H3_REFERENCE_LIMITS
    }
    return {kind: max(attached[kind], wired[kind]) for kind in H3_REFERENCE_LIMITS}


#: The range ``LoraLoaderModelOnly.strength_model`` declares. Outside it ComfyUI rejects
#: the whole prompt at ``/prompt`` validation, which reaches the Director as an opaque 502
#: after the submission round-trip — the same failure ``H3_REFERENCE_MAX_FRAMES`` exists to
#: prevent, so it is named here for the same reason and compared against the live schema by
#: ``tests/preflight_h3_ultra.py`` rather than being trusted as a literal.
H3_LORA_STRENGTH_LIMITS = (-100.0, 100.0)


@dataclass(frozen=True, slots=True)
class H3SamplingProfile:
    """One *whole* evidenced way to sample the H3 reference graph.

    A profile is a bundle, not a set of independent knobs: the LoRA, the strength it
    was applied at, the scheduler, the sampler and the step count were tuned together
    by whoever rendered them, and each field here is copied from one source of
    evidence. Mixing halves of two profiles produces a combination nobody has
    rendered while looking exactly as trustworthy as one that has been, which is why
    the adapter takes a profile name and never the individual values.

    ``lora`` and ``lora_strength`` are both ``None`` on a profile that applies no
    LoRA, and both set on one that does. A strength without a LoRA would read as
    "applied at 0.0" — a third state that means nothing — so it is rejected here
    rather than left to whoever adds the next profile.

    Every field is checked here, where it is declared, rather than at the point a
    payload is built. A profile with zero steps, an unnamed sampler or a strength
    outside the node's range produces a graph ComfyUI refuses at ``/prompt``
    validation — which the Director sees as an opaque 502 after the submission
    round-trip, at the end of the one path where the profile is *supposed* to be the
    trustworthy part. Failing at import, on the line that got it wrong, costs nothing.
    """

    lora: str | None
    lora_strength: float | None
    scheduler: str
    sampler: str
    steps: int

    def __post_init__(self) -> None:
        if (self.lora is None) != (self.lora_strength is None):
            raise ValueError(
                "An H3 sampling profile must name a LoRA and its strength together, "
                f"not {self.lora!r} at {self.lora_strength!r}"
            )
        # `bool` is an `int` in Python and `steps=True` would sample once, silently.
        if not isinstance(self.steps, int) or isinstance(self.steps, bool) or self.steps < 1:
            raise ValueError(
                f"An H3 sampling profile must sample at least one step, not {self.steps!r}"
            )
        for name, value in (("scheduler", self.scheduler), ("sampler", self.sampler)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"An H3 sampling profile's {name} must be named, not {value!r}"
                )
        if self.lora is None:
            return
        if not isinstance(self.lora, str) or not self.lora.strip():
            raise ValueError(
                f"An H3 sampling profile's LoRA must be a filename, not {self.lora!r}"
            )
        minimum, maximum = H3_LORA_STRENGTH_LIMITS
        if (
            not isinstance(self.lora_strength, (int, float))
            or isinstance(self.lora_strength, bool)
            or not math.isfinite(self.lora_strength)
            or not minimum <= self.lora_strength <= maximum
        ):
            raise ValueError(
                f"An H3 sampling profile's LoRA strength must be between {minimum:g} and "
                f"{maximum:g}, not {self.lora_strength!r}"
            )


#: The three configurations that have evidence behind them, each reproduced in full.
#:
#: ``default`` is the audited export
#: ``workflow_templates/reference_exports/h3-ultra-references-user-export.json``: node
#: ``2382`` schedules ``simple`` at 20 steps, node ``2388`` selects ``res_multistep``,
#: and its ``Power Lora Loader (rgthree)`` (node ``2383``) holds a turbo LoRA with
#: ``"on": false``. That off-switch is deliberate and is the whole reason this profile
#: applies no LoRA — the export is a 20-step graph that was rendered without one.
#:
#: ``turbo`` is the Director's own H3 stage in
#: ``…/user/default/workflows/Video/Music Video Advanced/04 - H3 Music Video - LTX 2.5
#: READY.json``: node ``5959`` (``LoraLoaderModelOnly``) applies
#: ``minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`` at 0.7 between the
#: ``ref2va`` UNET loader (node ``127``) and the sigma shift (node ``5960``), node
#: ``124`` schedules ``beta`` at 4 steps and node ``123`` selects ``euler``. That LoRA
#: is trained for the ``ref2v`` checkpoint this adapter already loads.
#:
#: ``turbo-references2v`` is the Director's *canonical* turbo reference-to-video graph —
#: the ComfyUI mode named ``MiniMaxH3Turbo References2V``, exported per mode on 2026-08-17
#: and audited as
#: ``workflow_templates/reference_exports/h3-turbo-references2v-user-export.json``: node
#: ``2390`` (``Power Lora Loader (rgthree)``) carries a single enabled entry —
#: ``minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors`` at strength 1.0 with
#: ``"on": true`` — on the same ``ref2va`` UNET (node ``2417``) this adapter loads; node
#: ``2408`` schedules ``simple`` at 8 steps and node ``2407`` selects ``euler``. Its sigma
#: shift (``12``/``3``) and disabled sage attention are what this adapter already emits.
#:
#: It is named after the graph it reproduces rather than after a speed claim, because it is
#: a *different bundle* from ``turbo`` rather than a correction of it: ``turbo`` comes from
#: the LTX 2.5 music-video pipeline, a different LoRA at a different strength with a
#: different scheduler and step count. Both are the Director's, both are evidenced, and
#: mixing them would produce exactly the unproven third combination this class exists to
#: prevent — so both ship, whole.
#:
#: Its LoRA reaches the graph through this adapter's ``LoraLoaderModelOnly`` rather than the
#: export's ``Power Lora Loader (rgthree)``; the reasoning is in
#: ``build_h3_reference_payload``. **Nothing has been rendered on this profile from this
#: application.** It is schema-audited only: its LoRA file is confirmed installed by
#: ``tests/preflight_h3_ultra.py``, which is not evidence of a frame.
#:
#: Only the bundles are proven. No LoRA on its own, and no scheduler, sampler or step count
#: borrowed across two profiles, has been rendered by anyone here.
H3_REFERENCE_PROFILES: dict[str, H3SamplingProfile] = {
    "default": H3SamplingProfile(
        lora=None,
        lora_strength=None,
        scheduler="simple",
        sampler="res_multistep",
        steps=20,
    ),
    "turbo": H3SamplingProfile(
        lora="minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
        lora_strength=0.7,
        scheduler="beta",
        sampler="euler",
        steps=4,
    ),
    "turbo-references2v": H3SamplingProfile(
        lora="minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors",
        lora_strength=1.0,
        scheduler="simple",
        sampler="euler",
        steps=8,
    ),
}

#: The profile a caller that names none gets: today's graph, unchanged.
H3_DEFAULT_PROFILE = "default"


def resolved_h3_sampling(profile: str, steps: int | None = None) -> H3SamplingProfile:
    """The bundle a reference submission actually samples on: the named profile, with an
    explicit step count substituted where the caller supplied one.

    Extracted from ``build_h3_reference_payload``'s own two lines and called by it, so that
    "what was recorded is what was submitted" is true by construction rather than by two call
    sites agreeing — which is `app.resolved_sampling_profile`'s argument one level up, and the
    reason that function exists at all.

    The substitution is the whole point of returning a profile rather than a name.
    ``H3Request.steps`` overrides the bundle's own count on every branch — "the profile chooses
    the graph, the Director chooses the effort" — so a submission whose *name* is ``turbo`` may
    have sampled twenty steps, and a record carrying only the name would be wrong about the one
    number the bundle is chosen for.

    An unknown profile is refused here, in the words the builder has always refused it in, and
    the type is checked as well as the membership for the builder's own reason: a non-string
    reaches ``profile in {...}`` as a ``TypeError``, which the route's ``except ValueError`` does
    not translate and which escapes as a 500.

    A supplied count is validated by ``H3SamplingProfile`` itself, on the line that got it wrong:
    ``steps=0`` would previously have built a graph that samples nothing. ``H3Request`` already
    bounds the field at 1..100, so no route can reach that; a script or a test can.
    """
    if not isinstance(profile, str) or profile not in H3_REFERENCE_PROFILES:
        raise ValueError(
            f"Unknown H3 sampling profile: {profile!r}; the profiles are "
            f"{', '.join(sorted(H3_REFERENCE_PROFILES))}"
        )
    sampling = H3_REFERENCE_PROFILES[profile]
    return sampling if steps is None else replace(sampling, steps=steps)


# --------------------------------------------------------------------------------------------
# Attention backend profiles — spec-h3-attention-backend, unmeasured.
# --------------------------------------------------------------------------------------------

#: The non-``model`` inputs each attention node class declares, in the order the live schema
#: publishes them. A profile whose keys do not match its class's exactly is refused at import:
#: an input name ComfyUI does not know reaches ``/prompt`` validation as an opaque 502 after
#: the submission round-trip, the same failure ``H3_REFERENCE_MAX_FRAMES`` exists to prevent.
#: Confirmed against live ``/object_info`` on ComfyUI 0.33.3, 2026-08-21, and re-checked there
#: by ``tests/preflight_h3_ultra.py`` rather than trusted from here.
H3_ATTENTION_NODE_INPUTS: dict[str, tuple[str, ...]] = {
    "PathchSageAttentionKJ": ("sage_attention", "allow_compile"),
    "ModelAttentionBackend": ("attention",),
}

#: The Python type each of those inputs takes, so ``allow_compile="False"`` — a string that is
#: truthy everywhere it is read — fails on the line that wrote it rather than silently enabling
#: ``torch.compile`` on a measurement run. ``sage_attention`` and ``attention`` are COMBO
#: strings; the pre-flight is what checks their *values* against the live option list.
H3_ATTENTION_INPUT_TYPES: dict[str, type] = {
    "sage_attention": str,
    "attention": str,
    "allow_compile": bool,
}

#: Where each attention node class sits relative to ``MiniMaxH3SigmaShift`` in the model patch
#: chain. **Read from the graphs, not chosen**, because node order in a patch chain is
#: semantic and each class arrives with its own evidence:
#:
#: - ``PathchSageAttentionKJ`` sits *after* the sigma shift in
#:   ``workflow_templates/reference_exports/h3-ultra-references-user-export.json`` and in every
#:   other H3 export this adapter reproduces — which is where this adapter already emits it,
#:   and why the default profile's bytes do not move.
#: - ``ModelAttentionBackend`` sits *before* the sigma shift in the Director's Comfy Kitchen
#:   graph, ``J:/Hermes-Remote/comfyui/workflowsbackup/ComfyKitchen/MiniMax-H3 TXT2VID IMG2VID
#:   (Full)- 20260818.json``. Chain-walked there on 2026-08-21 by MODEL link rather than by
#:   layout: node ``215`` (``UNETLoader``) → ``6006`` (``LoraLoaderModelOnly``, muted) → ``6088``
#:   (``Power Lora Loader (rgthree)``) → **``6095`` (``ModelAttentionBackend``, ``"comfy kitchen
#:   attention"``)** → ``6007`` (``MiniMaxH3SigmaShift`` 12/3) → ``214`` (``BasicGuider``) and
#:   ``212`` (``BasicScheduler``) → ``213`` (``SamplerCustomAdvanced``). That graph's *disabled*
#:   Sage branch (node ``223``, muted, ``["auto", False]``) hangs off the same Power Lora output
#:   and feeds its own muted sigma shift ``6097``, so the two attention nodes there are parallel
#:   branches rather than a stack.
#:
#: Whether the placement is load-bearing was checked in the implementations rather than assumed
#: from the picture, and the answer is that it is not — but only because exactly one attention
#: node is ever emitted. ``ModelAttentionBackend.patch`` calls
#: ``ModelPatcher.set_model_optimized_attention``, which writes
#: ``model_options["transformer_options"]["optimized_attention_override"]``
#: (``comfy/model_patcher.py:688``); ``PathchSageAttentionKJ.patch`` writes **the same key**
#: (``ComfyUI-KJNodes/nodes/model_optimization_nodes.py:134``). Two of them in one chain do not
#: compose — the later one wins, silently — which is the mechanical reason this registry emits
#: one node per profile and the reason the Kitchen graph branches instead of stacking. Given
#: that, the position each class is placed at is faithfulness to its own evidence rather than a
#: functional claim, and faithfulness is what this module trades on.
H3_ATTENTION_CHAIN_POSITION: dict[str, str] = {
    "PathchSageAttentionKJ": "after-shift",
    "ModelAttentionBackend": "before-shift",
}


@dataclass(frozen=True, slots=True)
class H3AttentionProfile:
    """One *whole* way for the H3 graph to compute attention: a node class and its values.

    A bundle for the same reason ``H3SamplingProfile`` is one. The two classes are not two
    settings of one knob — they come from different graphs, take different input names, sit at
    different points in the patch chain, and write the *same* model option, so a payload
    carrying both is a graph where one of them silently does nothing. Naming a profile picks
    the whole node; the individual values are never accepted from a caller.

    ``position`` is a property rather than a field on purpose: it is a property of the node
    class's evidence, not a knob a new profile may choose. See ``H3_ATTENTION_CHAIN_POSITION``.

    **None of these has been rendered.** Every profile here is schema-audited only — its class
    exists and its combo value is in the live option list, checked by
    ``tests/preflight_h3_ultra.py`` — and that is not evidence of a frame, still less of a
    frame whose mouth is in the right place. ``tests/measure_h3_attention.py`` is the
    measurement, and it has not been run.
    """

    class_type: str
    #: The non-``model`` inputs, as ordered pairs so the emitted node's key order is fixed and
    #: a digest can pin it. A mapping would be mutable and unhashable on a frozen dataclass.
    inputs: tuple[tuple[str, Any], ...]

    def __post_init__(self) -> None:
        expected = H3_ATTENTION_NODE_INPUTS.get(self.class_type)
        if expected is None:
            raise ValueError(
                f"An H3 attention profile must name a known attention node, not "
                f"{self.class_type!r}; the classes are "
                f"{', '.join(sorted(H3_ATTENTION_NODE_INPUTS))}"
            )
        names = tuple(name for name, _ in self.inputs)
        if names != expected:
            raise ValueError(
                f"{self.class_type} takes {expected}, not {names}"
            )
        for name, value in self.inputs:
            wanted = H3_ATTENTION_INPUT_TYPES[name]
            # `type(...) is` rather than `isinstance`: `bool` is an `int` and, more to the
            # point here, a caller who writes `allow_compile="False"` gets a truthy string.
            if type(value) is not wanted:
                raise ValueError(
                    f"{self.class_type}.{name} takes a {wanted.__name__}, not {value!r}"
                )
            if wanted is str and not value.strip():
                raise ValueError(f"{self.class_type}.{name} must be named, not {value!r}")

    @property
    def position(self) -> str:
        """Where this profile's node sits relative to the sigma shift."""
        return H3_ATTENTION_CHAIN_POSITION[self.class_type]

    def node(self, model_source: list[Any]) -> dict[str, Any]:
        """The single node this profile emits, drawing from ``model_source``."""
        return {
            "class_type": self.class_type,
            "inputs": {"model": model_source, **dict(self.inputs)},
        }


#: The attention backends a caller may select, each whole.
#:
#: ``default`` is what every H3 adapter here has always emitted: ``PathchSageAttentionKJ`` at
#: ``disabled`` with ``allow_compile`` off, faithful to the Director's 2026-08-17 exports. Read
#: the KJ node's source before reading that as "no acceleration": at ``disabled`` its ``patch``
#: is ``return model,`` — no clone, no model option written, nothing. **The default profile
#: does not select a backend; it declines to override the one ComfyUI was launched with.**
#:
#: On this machine that is *not* pytorch. ``/system_stats`` reports ``argv`` as
#: ``[main.py, --use-sage-attention, --fast]`` and ``ComfyUI/user/comfyui.log`` prints
#: ``Using sage attention`` at every start retained, including ``2026-08-20 09:00:34`` —
#: inside the serial overnight batch the render-cost cliff was reconstructed from. So the
#: cliff was measured **on SageAttention**, and the comparison arm nobody here has ever
#: rendered is plain ``pytorch``. That is why ``pytorch`` is a profile.
#:
#: ``pytorch`` and ``comfy-kitchen`` are ``ModelAttentionBackend``'s two published options,
#: read from live ``/object_info`` and re-checked by the pre-flight. ``comfy-kitchen`` is the
#: backend the Director's own Comfy Kitchen graph selects (node ``6095``, active); it is
#: ``comfy-kitchen``'s int8 attention (core maps the label to ``comfy_kitchen_int8``), so a
#: lip-sync check is not optional on it.
#:
#: **Two of the eight sage kernels are blessed, and the argument is the installed library, not
#: the option list.** ComfyUI's embedded python has ``sageattention 2.2.0+cu128torch2.7.1``
#: and no ``sageattn3`` package at all:
#:
#: - ``sage-auto`` (``auto``) calls the library's own dispatcher, and that dispatcher's sm120
#:   branch is explicit — ``sageattention/core.py:152`` routes Blackwell to
#:   ``sageattn_qk_int8_pv_fp8_cuda(pv_accum_dtype="fp32+fp16", qk_quant_gran="per_warp")``,
#:   commenting that "sm120 has accurate fp32 accumulator for fp8 mma". This is the vendor's
#:   own choice for this card, and it should reproduce ``default``'s timing — which makes it
#:   the experiment's internal control rather than a fifth guess.
#: - ``sage-fp8-cuda++`` names that same kernel and accumulator by hand. KJ's ``++`` mode
#:   passes ``pv_accum_dtype="fp32+fp16"`` and leaves ``qk_quant_gran`` at the library default
#:   ``per_thread`` (``model_optimization_nodes.py:46``), so it differs from ``sage-auto`` in
#:   exactly one term. If the two diverge, granularity is what diverged.
#:
#: The other five are deliberately **not reachable**, each for a stated reason, because a
#: registry that lists every option the schema publishes is a list of things nobody has
#: reasoned about:
#:
#: - ``sageattn3`` / ``sageattn3_per_block_mean`` import ``sageattn3``, a *separate* package
#:   (``model_optimization_nodes.py:51``) that is not installed. They would raise ImportError
#:   at sampling time — after the checkpoint is loaded and the GPU minutes are spent.
#: - ``sageattn_qk_int8_pv_fp16_triton``: the installed library's own sm120 comment says "the
#:   triton kernel is currently not usable on sm120".
#: - ``sageattn_qk_int8_pv_fp16_cuda`` and plain ``sageattn_qk_int8_pv_fp8_cuda``
#:   (``fp32+fp32``) are lower-precision-accumulator paths the vendor does not select for this
#:   architecture. Adding one is a one-line edit here plus a reason; that is the point.
#:
#: ``allow_compile`` stays ``False`` on every sage profile. It is a second variable, it needs
#: a ``torch.compile`` warmup this measurement does not budget for, and holding it constant is
#: what makes the sage arms comparable to each other.
H3_ATTENTION_PROFILES: dict[str, H3AttentionProfile] = {
    "default": H3AttentionProfile(
        class_type="PathchSageAttentionKJ",
        inputs=(("sage_attention", "disabled"), ("allow_compile", False)),
    ),
    "pytorch": H3AttentionProfile(
        class_type="ModelAttentionBackend",
        inputs=(("attention", "pytorch attention"),),
    ),
    "comfy-kitchen": H3AttentionProfile(
        class_type="ModelAttentionBackend",
        inputs=(("attention", "comfy kitchen attention"),),
    ),
    "sage-auto": H3AttentionProfile(
        class_type="PathchSageAttentionKJ",
        inputs=(("sage_attention", "auto"), ("allow_compile", False)),
    ),
    "sage-fp8-cuda++": H3AttentionProfile(
        class_type="PathchSageAttentionKJ",
        inputs=(("sage_attention", "sageattn_qk_int8_pv_fp8_cuda++"), ("allow_compile", False)),
    ),
}

#: The attention profile a caller that names none gets: today's node, today's values.
H3_DEFAULT_ATTENTION = "default"


def resolve_h3_attention(attention: str) -> H3AttentionProfile:
    """Look up an attention profile by name, refusing anything else.

    The type is checked as well as the membership for ``build_h3_reference_payload``'s
    reason: a caller that is not the route can pass an unhashable value, and
    ``attention in {...}`` would raise ``TypeError``, which no caller's ``except ValueError``
    translates. Silently falling back to the default would be worse than either — a
    measurement run that mistyped its profile would report the baseline's numbers under the
    candidate's name, which is the one outcome this whole experiment cannot survive.
    """
    if not isinstance(attention, str) or attention not in H3_ATTENTION_PROFILES:
        raise ValueError(
            f"Unknown H3 attention profile: {attention!r}; the profiles are "
            f"{', '.join(sorted(H3_ATTENTION_PROFILES))}"
        )
    return H3_ATTENTION_PROFILES[attention]


def _h3_attention_chain(
    attention: H3AttentionProfile, model_source: list[Any]
) -> tuple[dict[str, dict[str, Any]], list[Any]]:
    """The sigma shift and the attention patch, in the order the profile's evidence puts them.

    Returns the two nodes and the output the rest of the graph must read, so no caller has to
    know which of them ended up last. Shared by the reference and keyframe adapters, which
    carry the same 12/3 shift; the image-edit graph has no sigma shift at all and so has
    nothing to be ordered against — it emits ``profile.node`` directly.
    """
    shift_tail = {"shift_video": 12, "shift_audio": 3}
    if attention.position == "after-shift":
        return (
            {
                "mvp:shift": {
                    "class_type": "MiniMaxH3SigmaShift",
                    "inputs": {"model": model_source, **shift_tail},
                },
                "mvp:attention": attention.node(["mvp:shift", 0]),
            },
            ["mvp:attention", 0],
        )
    return (
        {
            "mvp:attention": attention.node(model_source),
            "mvp:shift": {
                "class_type": "MiniMaxH3SigmaShift",
                "inputs": {"model": ["mvp:attention", 0], **shift_tail},
            },
        },
        ["mvp:shift", 0],
    )


#: The frame ceiling ``MiniMaxH3ReferenceToVideo.length`` declares (about 150 s at 24 fps).
#: Above it ComfyUI rejects the whole prompt at ``/prompt`` validation with
#: ``value_bigger_than_max``, which reaches the Director as an opaque 502 after the submission
#: round-trip; refusing locally costs nothing and names the limit.
H3_REFERENCE_MAX_FRAMES = 3600


#: The aspect ratios ``ResolutionSelector`` declares, copied from
#: ``comfy_extras/nodes_resolution.py``'s ``ASPECT_RATIOS`` and confirmed against live
#: ``/object_info`` on 2026-08-18. Names are the combo's own strings because that is what the
#: Director's graph stores in node ``115`` of ``h3-ltx25-user-export.json``; a renamed key here
#: would stop matching the export it exists to reproduce.
H3_ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "1:1 (Square)": (1, 1),
    "2:3 (Portrait Photo)": (2, 3),
    "3:2 (Photo)": (3, 2),
    "3:4 (Portrait Standard)": (3, 4),
    "4:3 (Standard)": (4, 3),
    "9:16 (Portrait Widescreen)": (9, 16),
    "16:9 (Widescreen)": (16, 9),
    "21:9 (Ultrawide)": (21, 9),
}

#: The ranges ``ResolutionSelector`` declares for its two numeric inputs. Outside them ComfyUI
#: rejects the prompt at validation, the same failure ``H3_REFERENCE_MAX_FRAMES`` exists to
#: prevent, so they are refused locally for the same reason and checked against the live schema
#: by ``tests/preflight_h3_ultra.py``.
H3_MEGAPIXEL_LIMITS = (0.1, 16.0)
H3_MULTIPLE_LIMITS = (8, 128)

#: The width/height range ``MiniMaxH3ReferenceToVideo`` declares. A megapixel figure inside
#: ``ResolutionSelector``'s own range can still round to a frame H3 refuses — 16 MP at 21:9 is
#: 6208 px wide — so the *selected* size is checked against the node that has to accept it, not
#: only against the selector that produced it.
H3_REFERENCE_DIMENSION_LIMITS = (32, 16384)

#: The Director's own selection, read from node ``115`` (``ResolutionSelector``, titled
#: "Resolution Selector (Size)") of
#: ``workflow_templates/reference_exports/h3-ltx25-user-export.json``. Reproduced here rather
#: than approximated: the 2026-08-17 boundary run measured the H3 base this produced as
#: 1056x608 — ``33x32`` by ``19x32`` — and ``test_workflows.py`` pins that number.
H3_DEFAULT_MEGAPIXELS = 0.6
H3_DEFAULT_ASPECT_RATIO = "16:9 (Widescreen)"

#: The divisor the Director's selector uses, which is the same 32 the LTX boundary repair had
#: to discover the hard way (``LTX25_DIVISOR``, below) and the same step
#: ``MiniMaxH3ReferenceToVideo`` declares on ``width``/``height``. Asserted equal to
#: ``LTX25_DIVISOR`` in ``tests/test_workflows.py`` rather than aliased to it, because the two
#: are the same number for two different reasons and either could move alone.
H3_DEFAULT_MULTIPLE = 32


def select_resolution(
    *,
    megapixels: float = H3_DEFAULT_MEGAPIXELS,
    aspect_ratio: str = H3_DEFAULT_ASPECT_RATIO,
    multiple: int = H3_DEFAULT_MULTIPLE,
) -> tuple[int, int]:
    """Choose a frame the way the Director's ``ResolutionSelector`` chooses one.

    This is a line-for-line reproduction of ``comfy_extras.nodes_resolution``'s
    ``ResolutionSelector.execute``, read from the installed source rather than inferred from
    its outputs::

        total_pixels = megapixels * 1024 * 1024
        scale = sqrt(total_pixels / (w_ratio * h_ratio))
        width = round(w_ratio * scale / multiple) * multiple

    Three details in that are load-bearing and none of them are obvious:

    * the megapixel is **1024x1024**, not 1000000, so 0.6 MP is 629145.6 pixels;
    * the rounding is Python's ``round`` — banker's rounding, half to even — and reproducing
      it with ``math.floor(x + 0.5)`` would move sizes whose half-cell lands exactly on .5;
    * each axis rounds **independently**, so the result is on the grid but its ratio is only
      approximately the one asked for. 0.6 MP at 16:9 gives 1056x608, which is 1.737:1 and
      0.64 MP — larger than requested, and *not* 16:9. That drift is the node's behaviour and
      reproducing it faithfully matters more than being closer to the nominal figure.

    The rounding is returned, not hidden: callers get the size that will actually be sent, and
    the reference payload carries exactly these numbers, so what is rendered is what the
    manifest and the smoke read back.
    """
    if aspect_ratio not in H3_ASPECT_RATIOS:
        raise ValueError(
            f"Unknown aspect ratio: {aspect_ratio!r}; ResolutionSelector offers "
            f"{', '.join(H3_ASPECT_RATIOS)}"
        )
    # `bool` is an `int`, and `multiple=True` would round every axis to 1 px while looking like
    # a plausible flag at the call site. Type and range in one condition so both arrive as the
    # `ValueError` the route translates into a 422; a `TypeError` here would escape as a 500.
    minimum, maximum = H3_MULTIPLE_LIMITS
    if (
        not isinstance(multiple, int)
        or isinstance(multiple, bool)
        or not minimum <= multiple <= maximum
    ):
        raise ValueError(
            f"ResolutionSelector accepts a whole-number multiple between {minimum} and "
            f"{maximum}, not {multiple!r}"
        )
    if (
        not isinstance(megapixels, (int, float))
        or isinstance(megapixels, bool)
        or not math.isfinite(megapixels)
    ):
        raise ValueError(f"Megapixels must be a finite number, not {megapixels!r}")
    low, high = H3_MEGAPIXEL_LIMITS
    if not low <= megapixels <= high:
        raise ValueError(
            f"ResolutionSelector accepts between {low:g} and {high:g} megapixels, "
            f"not {megapixels:g}"
        )
    w_ratio, h_ratio = H3_ASPECT_RATIOS[aspect_ratio]
    total_pixels = megapixels * 1024 * 1024
    scale = math.sqrt(total_pixels / (w_ratio * h_ratio))
    width = round(w_ratio * scale / multiple) * multiple
    height = round(h_ratio * scale / multiple) * multiple
    # Checked after the arithmetic, against the node that receives the result: a legal
    # megapixel figure can still round to a frame `MiniMaxH3ReferenceToVideo` refuses, and
    # that refusal would otherwise arrive as an opaque 502 after the submission round-trip.
    floor, ceiling = H3_REFERENCE_DIMENSION_LIMITS
    for name, value in (("width", width), ("height", height)):
        if not floor <= value <= ceiling:
            raise ValueError(
                f"{megapixels:g} MP at {aspect_ratio} on a multiple of {multiple} selects "
                f"{width}x{height}, whose {name} is outside the H3 node's {floor}-{ceiling} "
                f"pixel range"
            )
    return width, height


#: The window a reference audio file is cut to, expressed as ``MiniMaxH3MediaLoader`` expresses
#: it. Established on 2026-08-18 from the installed node rather than assumed:
#:
#: * the loader's only input is ``media_state``, a JSON list, and each item may carry
#:   ``{"trim": {"start": s, "end": s}}`` in seconds. ``nodes.py``'s ``_trim`` reads those two
#:   keys and hands them to ``media_io.load_audio``, which slices the decoded waveform;
#: * ``_trim``'s ``num()`` keeps a value only when ``v > 0``, so a ``start`` of 0 is read by the
#:   loader as *no start* — but the ``end`` beside it survives, and ``_slice_audio`` reads a
#:   missing start as ``start or 0.0``. A window of ``{"start": 0.0, "end": 3.75}`` therefore
#:   slices ``[0, 3.75]``, which is exactly right. Sending it costs nothing and says what was
#:   meant;
#: * ``MiniMaxH3ReferenceToVideo`` has **no** window input of any kind. It is not the Director
#:   node and does not take ``start_second``/``end_second``. The loader's ``trim`` is the only
#:   place a reference audio window can be expressed at all.
#:
#: ``media_io._slice_audio`` clamps the end to the file's length (``b = min(total, ...)``), so a
#: window past the end of the song is silently shortened by the node. That is the clamp
#: ``song_audio_window`` refuses locally instead.
def song_audio_window(
    *, start: float, duration: float, song_duration: float
) -> dict[str, float]:
    """The ``trim`` a master-song reference carries for a shot at ``start``. Always a window.

    **Every** shot gets one, 0 s included. An earlier draft returned ``None`` at 0 s so that
    shot's payload stayed byte-identical to the pre-fix one, on the recorded premise that the
    conditioner already trimmed a whole-file reference to the render window — so 0 s was the
    case where the bug and the correct behaviour coincided. That premise is false:
    ``MiniMaxH3ReferenceToVideo._encode_ref_audio`` VAE-encodes the entire waveform and never
    truncates. Byte-identity at 0 s therefore preserved *the defect*, and preserved it for the
    shot most likely to exist in a fresh project — a 3.75 s shot at 0 s riding 154 seconds of
    song through every sampling step while its neighbour at 12 s rode 3.75. Renegotiated by the
    Director on 2026-08-18; see the Spec Change Log in ``spec-song-audio-window.md``.

    The consequence, stated plainly because it is easy to assume otherwise: **no reference
    payload carrying the master song is byte-identical to a pre-fix one any more.** Every one
    of them was conditioned on the whole track, so there is nothing left to preserve.

    ``song_duration`` of 0 means the stored Song never recorded a length. Nothing can be
    compared against an unknown, so no refusal is raised; the window is still sent. Refusing
    instead would block every project whose song length was never written, which is a different
    bug from the one this fixes.
    """
    _finite("Shot start", start)
    _finite("Shot duration", duration)
    _finite("Song duration", song_duration)
    if start < 0:
        raise ValueError(f"A shot cannot start before the song does, at {start:g}s")
    if duration <= 0:
        raise ValueError(f"A shot must last longer than zero seconds, not {duration:g}s")
    end = start + duration
    if song_duration > 0 and end > song_duration:
        raise ValueError(
            f"A shot from {start:g}s to {end:g}s runs past the end of the master song, "
            f"which is {song_duration:g}s long. The window would be silently shortened to "
            f"{max(0.0, song_duration - start):g}s of audio, so it is refused instead — "
            f"move or shorten the shot."
        )
    return {"start": start, "end": end}


def _resolve_frame(
    *,
    width: int | None,
    height: int | None,
    megapixels: float | None,
    aspect_ratio: str | None,
    multiple: int | None,
) -> tuple[int, int]:
    """One frame, described one way or the other, never both.

    The geometry rule ``build_h3_reference_payload`` established, extracted verbatim when
    ``build_h3_keyframe_payload`` needed the identical resolution: an explicit
    ``width``/``height`` pair is honoured exactly, the selector triple resolves through
    ``select_resolution``, supplying both is refused rather than resolved by precedence,
    and omitting all five takes the Director pipeline's measured 0.6 MP / 16:9 / 32.
    Extracted rather than copied so the two H3 adapters cannot drift apart on what a
    geometry request means — the digest pins in ``tests/test_workflows.py`` hold the
    reference payload byte-identical across the move.
    """
    explicit = {"width": width, "height": height}
    selected = {"megapixels": megapixels, "aspect_ratio": aspect_ratio, "multiple": multiple}
    given_explicit = [name for name, value in explicit.items() if value is not None]
    given_selected = [name for name, value in selected.items() if value is not None]
    if given_explicit and given_selected:
        raise ValueError(
            "A frame is described either by an explicit width and height or by megapixels, "
            f"aspect ratio and multiple — not both. This request sent "
            f"{', '.join(given_explicit)} and {', '.join(given_selected)}."
        )
    if given_explicit and len(given_explicit) != 2:
        # Half an explicit frame is not a frame. Without this, `width=640` alone would fall
        # through to the selector for its height and silently render 640x608.
        raise ValueError(
            f"An explicit frame needs both width and height; this request sent only "
            f"{given_explicit[0]}"
        )
    if given_explicit:
        # Type and range together, so both reach the route as the `ValueError` it turns into a
        # 422 rather than as a `TypeError` it does not catch.
        floor, ceiling = H3_REFERENCE_DIMENSION_LIMITS
        for name, value in explicit.items():
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not floor <= value <= ceiling
            ):
                raise ValueError(
                    f"An explicit {name} must be a whole number of pixels between {floor} "
                    f"and {ceiling}, which the H3 node accepts, not {value!r}"
                )
        # `given_explicit` has both names, so neither is None here.
        return width, height  # type: ignore[return-value]
    return select_resolution(
        megapixels=H3_DEFAULT_MEGAPIXELS if megapixels is None else megapixels,
        aspect_ratio=H3_DEFAULT_ASPECT_RATIO if aspect_ratio is None else aspect_ratio,
        multiple=H3_DEFAULT_MULTIPLE if multiple is None else multiple,
    )


def build_h3_reference_payload(
    *,
    prompt: str,
    references: list[dict[str, Any]],
    duration: float,
    seed: int,
    prefix: str,
    width: int | None = None,
    height: int | None = None,
    megapixels: float | None = None,
    aspect_ratio: str | None = None,
    multiple: int | None = None,
    steps: int | None = None,
    ref_image_size: str = "match",
    profile: str = H3_DEFAULT_PROFILE,
    attention: str = H3_DEFAULT_ATTENTION,
) -> dict[str, dict[str, Any]]:
    """Build the H3 Ultra references-to-video path in one of its evidenced profiles.

    ``profile`` selects a whole configuration from ``H3_REFERENCE_PROFILES`` — LoRA,
    strength, scheduler, sampler and step default together. The default profile emits
    exactly the graph this builder emitted before profiles existed; a profile carrying a
    LoRA inserts one ``LoraLoaderModelOnly`` between ``mvp:model`` and ``mvp:shift`` so
    everything downstream draws from the LoRA rather than the loader.

    That node class is the Director's own on the ``turbo`` profile's source graph. It is
    **not** the class ``turbo-references2v``'s export uses: that one carries its LoRA in a
    ``Power Lora Loader (rgthree)``, and reproducing that class here was considered and
    deliberately rejected. Live ``/object_info`` declares *no* ``lora_*`` inputs on it —
    its ``required`` map is empty and only ``model``/``clip`` are optional, because its
    LoRA rows are client-side widgets — so a faithful copy would put the filename and the
    strength where the schema cannot see them: the pre-flight could no longer confirm the
    file is installed or the strength is in range, and would report the reproduction's own
    widget keys as inputs that do not exist. One enabled row in that loader is the same
    model patch ``LoraLoaderModelOnly`` applies, so the cost of the substitution is that
    ``turbo-references2v`` is evidenced in its *values* and not in its wiring. Said here
    because an unstated substitution is the part that would mislead.

    ``steps`` left as ``None`` takes the profile's own count — the number its evidence
    was rendered at. A count supplied here overrides it: the profile chooses the graph,
    the Director chooses the effort.

    ``attention`` selects a whole attention node from ``H3_ATTENTION_PROFILES`` — the class,
    its values and its place in the patch chain together. It is orthogonal to ``profile``:
    one names how the model is sampled, the other how its attention is computed. The default
    emits the node this builder has always emitted, in the position it has always been in.

    **Geometry comes one way or the other, never both.** ``width``/``height`` are an explicit
    frame and are honoured exactly — a caller asking for 640x384 gets 640x384, byte for byte,
    which is what the pinned smoke and every existing test depend on. ``megapixels`` /
    ``aspect_ratio`` / ``multiple`` are the control the Director's own ``ResolutionSelector``
    exposes and are resolved through ``select_resolution``. Supplying both is refused rather
    than resolved by precedence: a caller who sent 0.6 MP *and* 640x384 has two different
    intentions in one request, and silently honouring either one produces a render nobody
    asked for while looking like a render somebody did.

    Omitting all five takes ``H3_DEFAULT_MEGAPIXELS`` at ``H3_DEFAULT_ASPECT_RATIO`` on
    ``H3_DEFAULT_MULTIPLE`` — 1056x608, the size the Director's pipeline actually produces on
    this machine. That is a **change** from the 1344x768 this builder's callers defaulted to
    before, and it is deliberate: 1344x768 was never measured against the Director's graph,
    while 1056x608 was.
    """
    # Before anything else in this function, so an unknown profile is refused before a
    # payload is built rather than after the references validate. Both the membership check and
    # the step substitution live in `resolved_h3_sampling`, which `app.generate_h3` also calls to
    # *record* what this submission ran on — one resolution, so the graph and the provenance
    # cannot disagree about the bundle or about its step count.
    sampling = resolved_h3_sampling(profile, steps)
    # Resolved here, beside the sampling profile and before any reference is looked at, for
    # the same reason: a mistyped backend name must fail on the request rather than on
    # whichever unrelated thing it happens to trip over next.
    attention_profile = resolve_h3_attention(attention)
    sampled_steps = sampling.steps
    # Geometry, resolved once and before any reference is looked at, so a request carrying
    # two contradictory ways to say how big the frame is fails on that rather than on
    # whichever unrelated thing it happens to trip over next. The rule itself lives in
    # `_resolve_frame`, shared with the keyframe adapter.
    width, height = _resolve_frame(
        width=width,
        height=height,
        megapixels=megapixels,
        aspect_ratio=aspect_ratio,
        multiple=multiple,
    )
    # The partition, resolved once and used three times — for the ceilings below, for the
    # emptiness check under them, and for the wiring itself. One call because the bug this
    # replaced was two implementations of the loader's grouping that had drifted: the ceilings
    # counted raw kinds while the wiring counted its own indices, and neither counted what the
    # node would actually put in each group. See `partition_h3_references`.
    slots = partition_h3_references(references)
    counts = h3_reference_group_counts(references, slots)
    for kind, limit in H3_REFERENCE_LIMITS.items():
        if counts[kind] > limit:
            # Names the kind that overflowed and the number actually counted, because the
            # route appends the master song as a further `audio` reference: a Director who
            # attached exactly three audio Assets and ticked "use song" was previously told
            # "at most 3 standalone audios" while looking at three of them.
            song = " (the master song counts as one)" if kind == "audio" else ""
            raise ValueError(
                f"H3 accepts at most {limit} {kind} references per shot and this one has "
                f"{counts[kind]}{song}"
            )
    if not references:
        raise ValueError("At least one H3 reference is required")
    if not slots:
        # References were attached and every one of them is switched off, so the loader would
        # hand the splitter four empty groups and the conditioner would be wired to no media at
        # all — the "it would sample noise" failure the line above exists against, reached by a
        # different route and therefore said in different words. Refused here rather than at the
        # ceilings, because nothing overflowed.
        raise ValueError(
            f"All {len(references)} H3 references are switched off, so the render would be "
            f"conditioned on no media at all"
        )
    if ref_image_size not in {"match", "max"}:
        raise ValueError("ref_image_size must be 'match' or 'max'")
    _finite("Shot duration", duration)
    # The over-render margin (spec-monitor-and-over-render): every take is rendered at
    # least half a second longer than its window, grid-snapped, so editable room exists at
    # either end. This deliberately changed every reference payload's `length` on
    # 2026-08-19 — the digests in `tests/test_workflows.py` were re-pinned with it.
    length = over_render_frames(duration)
    if length > H3_REFERENCE_MAX_FRAMES:
        raise ValueError(
            f"A {duration:g}s shot needs {length} frames (over-render margin included), "
            f"above the H3 node's {H3_REFERENCE_MAX_FRAMES}-frame maximum "
            f"({H3_REFERENCE_MAX_FRAMES / H3_FRAME_RATE:g}s at {H3_FRAME_RATE} fps)"
        )
    # Every `trim` that reaches `media_state`, checked here rather than trusted from the
    # caller. The loader's `_trim` silently drops anything it cannot read as a positive
    # number — a string, a `null`, a negative — so a malformed window does not fail, it
    # renders the whole file and looks exactly like a shot that asked for no window. That is
    # the failure mode this whole story exists to remove, so it is refused where it is
    # visible instead. See `song_audio_window` for how the loader expresses a window at all.
    for item in references:
        window = item.get("trim")
        if window is None:
            continue
        # Both keys required rather than defaulted: the loader reads them independently and a
        # trim carrying only one is a half-window that renders without complaint.
        if not isinstance(window, dict) or {"start", "end"} - set(window):
            raise ValueError(
                f"A reference trim must name both start and end seconds, not {window!r}"
            )
        window_start = _finite("Reference trim start", window["start"])
        window_end = _finite("Reference trim end", window["end"])
        if window_start < 0:
            raise ValueError(f"A reference trim cannot start before 0s, at {window_start:g}s")
        if window_end <= window_start:
            raise ValueError(
                f"A reference trim from {window_start:g}s to {window_end:g}s selects nothing"
            )
    media_state = json.dumps(
        [{**item, "enabled": item.get("enabled", True)} for item in references],
        separators=(",", ":"),
    )
    condition_inputs: dict[str, Any] = {
        "clip": ["mvp:clip", 0], "vae": ["mvp:video_vae", 0],
        "audio_vae": ["mvp:audio_vae", 0], "prompt": prompt,
        "width": width, "height": height, "length": length,
        "ref_image_size": ref_image_size,
    }
    # One statement of the wiring, over the partition the ceilings were counted from: the
    # conditioner input, the splitter output index and the group a reference landed in all come
    # from the same `H3ReferenceSlot`, so a slot the node did not fill cannot be wired and a slot
    # it did fill cannot be missed. This loop used to count its own picture/video/audio indices
    # and knew nothing about `enabled` or about a standalone video's soundtrack, which put every
    # later reference of its kind on an output the loader had left empty.
    for entry in slots:
        condition_inputs[H3_SPLIT_INPUTS[entry.group].format(slot=entry.slot)] = [
            "mvp:split", H3_SPLIT_OFFSETS[entry.group] + entry.slot,
        ]
    # Every node downstream of the loader reads this one name, so a profile that inserts
    # a LoRA moves the whole graph onto it rather than leaving a node behind on the
    # unpatched model — which would silently sample half-adapted.
    model_source = ["mvp:model", 0] if sampling.lora is None else ["mvp:lora", 0]
    # The shift and the attention patch, in the attention profile's evidenced order, and the
    # output everything downstream reads — which is `mvp:attention` on the default profile,
    # exactly as it was before an attention profile existed.
    patch_chain, patched_model = _h3_attention_chain(attention_profile, model_source)
    return {
        "mvp:model": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        # Absent entirely on a profile with no LoRA: an unpacked empty mapping adds no
        # key, so the default profile's payload is what it always was, byte for byte.
        **(
            {}
            if sampling.lora is None
            else {
                "mvp:lora": {
                    "class_type": "LoraLoaderModelOnly",
                    "inputs": {
                        "model": ["mvp:model", 0],
                        "lora_name": sampling.lora,
                        "strength_model": sampling.lora_strength,
                    },
                }
            }
        ),
        **patch_chain,
        "mvp:preview": {"class_type": "ModelPreviewOverrideKJ", "inputs": {"model": patched_model, "max_resolution": 1024, "jpeg_quality": 80, "suppress_default_preview": True, "preview_frames": 12, "preview_fps": 12}},
        "mvp:clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "type": "minimax", "device": "default"}},
        "mvp:video_vae": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "mvp:audio_vae": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "mvp:references": {"class_type": "MiniMaxH3MediaLoader", "inputs": {"media_state": media_state}},
        "mvp:split": {"class_type": "MiniMaxH3ReferenceSplitter", "inputs": {"references": ["mvp:references", 0]}},
        "mvp:condition": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": condition_inputs},
        "mvp:sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampling.sampler}},
        "mvp:scheduler": {"class_type": "BasicScheduler", "inputs": {"model": ["mvp:preview", 0], "scheduler": sampling.scheduler, "steps": sampled_steps, "denoise": 1.0}},
        "mvp:noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "mvp:guider": {"class_type": "BasicGuider", "inputs": {"model": ["mvp:preview", 0], "conditioning": ["mvp:condition", 0]}},
        "mvp:sample": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["mvp:noise", 0], "guider": ["mvp:guider", 0], "sampler": ["mvp:sampler", 0], "sigmas": ["mvp:scheduler", 0], "latent_image": ["mvp:condition", 1]}},
        "mvp:video": {"class_type": "VAEDecode", "inputs": {"samples": ["mvp:sample", 0], "vae": ["mvp:video_vae", 0]}},
        "mvp:audio": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["mvp:sample", 0], "vae": ["mvp:audio_vae", 0]}},
        "mvp:save": {"class_type": "VHS_VideoCombine", "inputs": {"images": ["mvp:video", 0], "audio": ["mvp:audio", 0], "frame_rate": 24, "loop_count": 0, "filename_prefix": prefix, "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 19, "save_metadata": True, "trim_to_audio": False, "pingpong": False, "save_output": True}},
    }


#: The frame ceiling ``MiniMaxH3ImageToVideo.length`` declares, read from live
#: ``/object_info`` on 2026-08-18. The same 3600 ``MiniMaxH3ReferenceToVideo`` declares, and
#: deliberately *not* an alias of ``H3_REFERENCE_MAX_FRAMES``: the two adapters restate the
#: same ceiling for their own nodes, and aliasing would make a divergence in one invisible
#: in the other's audit — ``tests/preflight_h3_ultra.py`` checks each against its own node.
H3_KEYFRAME_MAX_FRAMES = 3600

#: The step count a keyframe render takes when the request names none, read from the
#: audited export's own ``BasicScheduler`` (``h3-first-last-user-export.json``, node
#: ``2345``) and pinned against it by chain-walk in ``tests/test_workflows.py``. This path
#: is deliberately offered **no sampling profile**: the turbo exports are T2V, Director and
#: References2V graphs on other checkpoints, no turbo evidence exists for ``fl2va``, and a
#: profile here would be exactly the unevidenced blend the profile machinery exists to
#: prevent. Marked Ask First in the spec and not asked.
H3_KEYFRAME_DEFAULT_STEPS = 20


def build_h3_keyframe_payload(
    *,
    prompt: str,
    first_frame: str,
    last_frame: str | None,
    duration: float,
    seed: int,
    prefix: str,
    width: int | None = None,
    height: int | None = None,
    megapixels: float | None = None,
    aspect_ratio: str | None = None,
    multiple: int | None = None,
    steps: int | None = None,
    attention: str = H3_DEFAULT_ATTENTION,
) -> dict[str, dict[str, Any]]:
    """Build the MiniMax H3 first/last keyframe graph (the Director's ``I2V-FLframe`` mode).

    Derived from ``workflow_templates/reference_exports/h3-first-last-user-export.json`` —
    28 nodes of which **27 are reachable** from its single ``VHS_VideoCombine``, and the one
    orphan is the inherited ``ref2va`` ``UNETLoader``. The reachable checkpoint is
    ``minimax_h3_fl2va_pruned_int8_convrot.safetensors``, a dedicated first/last model, and
    this builder must never load ``ref2va``: that would be an unevidenced combination *and*
    a ~20 GB model swap against every reference render. ``tests/test_workflows.py`` pins
    every bundle value here by chain-walking the export rather than restating it.

    **What live ``/object_info`` settles about this graph, read 2026-08-18 and load-bearing
    for two modes' honesty:** ``MiniMaxH3ImageToVideo`` requires ``clip`` / ``vae`` /
    ``prompt`` / ``width`` / ``height`` / ``length`` and declares ``first_frame`` and
    ``last_frame`` both **optional** — so a first-frame-only graph is schema-legal, which is
    what lets ``image_to_video`` route here with ``last_frame`` simply absent. The node has
    **no reference-audio input of any kind**: no ``ref_audios`` autogrow group, no audio
    input anywhere on it or its conditioning path. A keyframe shot therefore **cannot be
    conditioned on the master song and cannot lip-sync to it** — its audio track is
    generated by the sampler and decoded through the audio VAE, exactly like the text-only
    Director path's. ``SHOT_MODE_SPECS`` declares ``song_audio=False`` on both modes for
    this reason, and ``use_song_audio`` on a keyframe shot is refused in
    ``mode_specification_problems``' words rather than silently dropped.
    ``tests/preflight_h3_ultra.py`` re-checks both schema facts against the live server.

    ``last_frame=None`` is the ``image_to_video`` shape and omits the input entirely —
    the schema treats an absent optional as absent, and sending a null would be a shape
    nobody evidenced. An *empty string* for either frame is refused: a caller that passed
    ``""`` lost a path, and treating it as "no frame" would silently change which graph
    renders.

    Two reachable node groups from the export are deliberately not reproduced, both
    mirroring decisions the shipped reference adapter already made against identical
    evidence — a silent divergence between two H3 adapters on the same node is the drift
    the profile work exists to prevent:

    * ``SpectrumApplyMiniMaxH3`` sits **enabled** in this export's model chain, exactly as
      it sits enabled in ``h3-ultra-references-user-export.json`` and
      ``h3-references2v-user-export.json`` — and ``build_h3_reference_payload`` omits it
      there. Its parameters are marked Ask First in the spec and have not been asked, so
      this adapter omits it the same way, deliberately: the model chain runs loader →
      sigma shift → sage attention → preview, with no Spectrum node. The export's empty
      ``Power Lora Loader (rgthree)`` is dropped for the reference adapter's recorded
      reason too — live schema declares no ``lora_*`` inputs on it, and an empty pass-through
      patches nothing.
    * The export's geometry plumbing — ``ImageScaleToTotalPixels`` (1 MP) →
      ``GetImageSize`` → two ``Any Switch (rgthree)`` with a 0.9 MP ``ResolutionSelector``
      fallback, and the ``ComfyMathExpression`` frame counter — is editor-side convenience
      resolved server-side here, as the reference adapter resolves the *same* plumbing in
      its own exports. **Keyframe shots share ``select_resolution``'s 0.6 MP default**, and
      the reason is stated rather than assumed: the export's in-graph sizing derives an
      unmeasured ~1 MP frame from whatever the first image happens to be, while 0.6 MP /
      16:9 / 32 is the one geometry measured from the Director's own pipeline on this
      machine (1056x608) and the default every other H3 reference render already gets.
      Giving the keyframe modes a different default would make one mode's renders a
      different, unevidenced size for no reason a manifest could show. The frame count is
      the export's own expression — ``max(5, round(a * 24))`` snapped up to the 17k+5 grid —
      computed here exactly as ``build_h3_reference_payload`` computes it.

    ``steps`` left as ``None`` takes ``H3_KEYFRAME_DEFAULT_STEPS``, the export's own count.
    There is no *sampling* ``profile`` parameter on purpose — see that constant. ``attention``
    is a different question and is offered: it names how attention is computed rather than
    what is sampled, so unlike a LoRA bundle it carries no unevidenced blend of this graph
    with someone else's. The default emits this graph's own two patch nodes unchanged.
    """
    # Refused before anything else, so a mistyped backend fails on the request rather than on
    # whichever unrelated thing it happens to trip over next.
    attention_profile = resolve_h3_attention(attention)
    # Both checks are one condition apiece so a wrong type and a blank string alike arrive
    # as the `ValueError` the route translates into a 422 — a `TypeError` would escape as a
    # 500, the same reasoning `select_resolution` records for its own combined conditions.
    if not isinstance(first_frame, str) or not first_frame.strip():
        raise ValueError(
            f"A keyframe shot's first frame must be a file path, not {first_frame!r}"
        )
    if last_frame is not None and (not isinstance(last_frame, str) or not last_frame.strip()):
        raise ValueError(
            f"A keyframe shot's last frame must be a file path, not {last_frame!r}; "
            f"a shot with no last frame passes None, never an empty string"
        )
    width, height = _resolve_frame(
        width=width,
        height=height,
        megapixels=megapixels,
        aspect_ratio=aspect_ratio,
        multiple=multiple,
    )
    _finite("Shot duration", duration)
    # The over-render margin, same rule and same date as the reference builder's — one
    # policy across every shot adapter, computed by one function.
    length = over_render_frames(duration)
    if length > H3_KEYFRAME_MAX_FRAMES:
        raise ValueError(
            f"A {duration:g}s shot needs {length} frames (over-render margin included), "
            f"above the H3 keyframe node's {H3_KEYFRAME_MAX_FRAMES}-frame maximum "
            f"({H3_KEYFRAME_MAX_FRAMES / H3_FRAME_RATE:g}s at {H3_FRAME_RATE} fps)"
        )
    # The frames travel as `picture` entries of the same media loader the reference path
    # uses, in a **fixed** order the conditioner wiring depends on: entry 0 is the first
    # frame, entry 1 the last. The labels name the roles so a ComfyUI-side log reads as the
    # shot does; which file plays which role is decided by the named parameters above,
    # never by the order a caller happened to list citations in.
    # The shift and the attention patch, in the attention profile's evidenced order. Same
    # helper and same 12/3 shift as the reference adapter; the default emits this graph's
    # existing two nodes, in their existing order, byte for byte.
    patch_chain, patched_model = _h3_attention_chain(attention_profile, ["mvp:model", 0])
    frames = [{"kind": "picture", "file": first_frame, "label": "first frame", "enabled": True}]
    if last_frame is not None:
        frames.append(
            {"kind": "picture", "file": last_frame, "label": "last frame", "enabled": True}
        )
    condition_inputs: dict[str, Any] = {
        "clip": ["mvp:clip", 0], "vae": ["mvp:video_vae", 0], "prompt": prompt,
        "width": width, "height": height, "length": length,
        # The splitter's picture outputs, through the same audited offsets the reference
        # adapter wires — the export feeds `first_frame` from splitter output 0 and
        # `last_frame` from output 1, which are `picture_1` and `picture_2` in the live
        # schema's own output names.
        "first_frame": ["mvp:split", H3_SPLIT_OFFSETS["picture"]],
    }
    if last_frame is not None:
        condition_inputs["last_frame"] = ["mvp:split", H3_SPLIT_OFFSETS["picture"] + 1]
    return {
        "mvp:model": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        **patch_chain,
        "mvp:preview": {"class_type": "ModelPreviewOverrideKJ", "inputs": {"model": patched_model, "max_resolution": 1024, "jpeg_quality": 80, "suppress_default_preview": True, "preview_frames": 12, "preview_fps": 12}},
        "mvp:clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "type": "minimax", "device": "default"}},
        "mvp:video_vae": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "mvp:audio_vae": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "mvp:frames": {"class_type": "MiniMaxH3MediaLoader", "inputs": {"media_state": json.dumps(frames, separators=(",", ":"))}},
        "mvp:split": {"class_type": "MiniMaxH3ReferenceSplitter", "inputs": {"references": ["mvp:frames", 0]}},
        "mvp:condition": {"class_type": "MiniMaxH3ImageToVideo", "inputs": condition_inputs},
        "mvp:sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "mvp:scheduler": {"class_type": "BasicScheduler", "inputs": {"model": ["mvp:preview", 0], "scheduler": "simple", "steps": H3_KEYFRAME_DEFAULT_STEPS if steps is None else steps, "denoise": 1.0}},
        "mvp:noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "mvp:guider": {"class_type": "BasicGuider", "inputs": {"model": ["mvp:preview", 0], "conditioning": ["mvp:condition", 0]}},
        "mvp:sample": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["mvp:noise", 0], "guider": ["mvp:guider", 0], "sampler": ["mvp:sampler", 0], "sigmas": ["mvp:scheduler", 0], "latent_image": ["mvp:condition", 1]}},
        "mvp:video": {"class_type": "VAEDecode", "inputs": {"samples": ["mvp:sample", 0], "vae": ["mvp:video_vae", 0]}},
        "mvp:audio": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["mvp:sample", 0], "vae": ["mvp:audio_vae", 0]}},
        "mvp:save": {"class_type": "VHS_VideoCombine", "inputs": {"images": ["mvp:video", 0], "audio": ["mvp:audio", 0], "frame_rate": 24, "loop_count": 0, "filename_prefix": prefix, "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 19, "save_metadata": True, "trim_to_audio": False, "pingpong": False, "save_output": True}},
    }


# --------------------------------------------------------------------------------------------
# The H3 image-edit adapter (AI Mod) — spec-ai-mod-image-edit, evidence
# `h3-image-edit-user-export.json` / `h3-turbo-image-edit-user-export.json`.
# --------------------------------------------------------------------------------------------

#: The two evidenced image-edit bundles, one per export, whole. ``default`` is the plain
#: export's own `simple`/20/`res_multistep` with an empty Power Lora. ``turbo`` is its twin's
#: `minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors` at 1.0 with **`beta`**/8/
#: `euler` — note the scheduler differs from `turbo-references2v`'s `simple` with the *same*
#: LoRA. Same class, different graphs, different scheduler: bundles are evidenced per export
#: and never borrowed across (the MANIFEST records both readings).
H3_IMAGE_EDIT_PROFILES: dict[str, H3SamplingProfile] = {
    "default": H3SamplingProfile(
        lora=None,
        lora_strength=None,
        scheduler="simple",
        sampler="res_multistep",
        steps=20,
    ),
    "turbo": H3SamplingProfile(
        lora="minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors",
        lora_strength=1.0,
        scheduler="beta",
        sampler="euler",
        steps=8,
    ),
}

#: The export's own canvas. Its two rgthree Any Switches offer an INTConstant against the
#: scaled source's dimensions, and the switch returns its first non-null input at runtime —
#: the constants — so 1920×1080 is what the export actually renders, resolved server-side
#: rather than reproduced as plumbing. (1080 is not a multiple of 32; the latent node
#: accepts it, and honesty to the evidence beats tidiness to a grid nothing here enforces.)
H3_IMAGE_EDIT_DEFAULT_WIDTH = 1920
H3_IMAGE_EDIT_DEFAULT_HEIGHT = 1080

#: The conditioner's picture ceiling, same node and same nine slots as the reference path.
H3_IMAGE_EDIT_MAX_PICTURES = 9

#: How a hand-written full-form prompt announces itself. An instruction carrying this is
#: sent verbatim — wrapping it again would nest one structured document inside another.
IMAGE_EDIT_STRUCTURED_MARKER = "subject_definitions:"


def image_edit_prompt(
    instruction: str,
    *,
    source_kind: str,
    source_label: str,
    extra_labels: tuple[str, ...] = (),
) -> str:
    """The edit instruction in the workflow's own prompting form.

    The shape is the guide bundled inside the Director's editor-format workflow (referenced,
    never copied wholesale): subject definitions, a ``[reference generation]`` summary,
    ``attribute_transfer`` on the base picture, ``fully_preserved`` identity, the edit as
    the detailed description, and the two sound fields closed off (``Silence`` / ``N/A``)
    because this graph saves an image. The order is the guide's own rule: *reference →
    preserve identity → describe the edit → preserve everything else* — and the
    always-preserve list deliberately **excludes clothing and pose**, because "change the
    outfit" is the Director's own worked example and a preamble that pre-forbids the edit
    would fight the instruction it wraps.

    A character source gets the guide's subject form; any other kind gets the plain form —
    a warehouse has no identity to preserve, only composition and style. An instruction
    that already carries ``subject_definitions:`` is returned verbatim.
    """
    if IMAGE_EDIT_STRUCTURED_MARKER in instruction:
        return instruction
    edit = instruction.strip()
    extras = "".join(
        f"\n<Picture {index}> is {label}." for index, label in enumerate(extra_labels, start=2)
    )
    extra_retention = "".join(
        f"\n<Picture {index}>: reference - additional visual reference."
        for index in range(2, 2 + len(extra_labels))
    )
    if source_kind == "character":
        return (
            "subject_definitions:\n"
            "<Picture 1> is the base image being edited.\n"
            f"<Subject 1> is the character from <Picture 1> ({source_label}), preserving "
            "the character's identity and important visual features."
            f"{extras}\n\n"
            "summary:\n"
            "[reference generation] The target image is an edited version of <Picture 1>.\n\n"
            "retention_analysis:\n"
            "<Picture 1>: attribute_transfer - provides the original composition, visual "
            "style, and subject reference.\n"
            "<Subject 1>: fully_preserved - preserve the character's identity and important "
            "design features."
            f"{extra_retention}\n\n"
            "detailed_description:\n"
            f"Preserve <Subject 1>'s exact identity, face, and body proportions. {edit} "
            "Keep everything not named above unchanged: composition, lighting, background, "
            "and visual style.\n\n"
            "overall_soundscape:\n"
            "Silence\n\n"
            "non_diegetic_music:\n"
            "N/A"
        )
    return (
        "subject_definitions:\n"
        f"<Picture 1> is the base image being edited ({source_label})."
        f"{extras}\n\n"
        "summary:\n"
        "[reference generation] The target image is an edited version of <Picture 1>.\n\n"
        "retention_analysis:\n"
        "<Picture 1>: attribute_transfer - provides the original composition, visual "
        "style, and subject reference."
        f"{extra_retention}\n\n"
        "detailed_description:\n"
        f"{edit} Keep everything not named above unchanged: composition, lighting, and "
        "visual style.\n\n"
        "overall_soundscape:\n"
        "Silence\n\n"
        "non_diegetic_music:\n"
        "N/A"
    )


def build_h3_image_edit_payload(
    *,
    prompt: str,
    pictures: list[dict[str, Any]],
    seed: int,
    width: int | None = None,
    height: int | None = None,
    steps: int | None = None,
    profile: str = "default",
    attention: str = H3_DEFAULT_ATTENTION,
    prefix: str,
) -> dict[str, dict[str, Any]]:
    """Build the H3 image-edit graph: a ``length: 5`` reference render that saves one image.

    Reproduces the reachable 24 of `h3-image-edit-user-export.json`'s 27 nodes. What is
    *not* reproduced is a named decision: the Image Comparer (editor UI), the video VAE
    loader and the ``fl2va`` UNET (the export's orphans — the exact mirror of the keyframe
    export, where ``ref2va`` was the orphan), Spectrum (both prior H3 adapters' recorded
    omission, mirrored again), the empty Power Lora (a no-op; a profile's LoRA arrives
    through ``LoraLoaderModelOnly`` as everywhere else), and the INTConstant/Any Switch
    plumbing (resolved server-side; see ``H3_IMAGE_EDIT_DEFAULT_WIDTH``). **No sigma
    shift and no preview node exist in this export** — this builder emits what the
    evidence holds, not the video graphs' habit.

    The distinctive wiring, each read from the export: the dedicated **image VAE**
    (``t1_image_vae_step1597``) sits in the conditioner's ``vae`` seat *and* decodes the
    sample; the first picture — the base image being edited — passes through
    ``ImageScaleToTotalPixelsX`` (2 MP, ×32, crop, lanczos) into ``ref_image_0`` while
    pictures 2–9 ride the splitter unscaled; and the sampler's latent is an
    ``EmptyMiniMaxH3ImageLatentAV`` canvas, not the conditioner's output.

    ``attention`` selects a whole attention node from ``H3_ATTENTION_PROFILES``. **There is no
    sigma shift in this export**, so this graph's attention node has nothing to be ordered
    against and every profile's node lands in the one slot — between the model source and the
    scheduler and guider, where this builder's ``PathchSageAttentionKJ`` already sits.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("An image edit requires a prompt")
    attention_profile = resolve_h3_attention(attention)
    if profile not in H3_IMAGE_EDIT_PROFILES:
        named = ", ".join(sorted(H3_IMAGE_EDIT_PROFILES))
        raise ValueError(f"Unknown H3 image-edit profile {profile!r}; expected one of: {named}")
    sampling = H3_IMAGE_EDIT_PROFILES[profile]
    if steps is not None and (not isinstance(steps, int) or isinstance(steps, bool) or steps < 1):
        raise ValueError(f"steps must be a positive integer, not {steps!r}")
    if not pictures:
        raise ValueError("An image edit requires at least the base picture")
    if len(pictures) > H3_IMAGE_EDIT_MAX_PICTURES:
        raise ValueError(
            f"H3 accepts at most {H3_IMAGE_EDIT_MAX_PICTURES} pictures per edit and this "
            f"one has {len(pictures)}"
        )
    for item in pictures:
        if not isinstance(item.get("file"), str) or not item["file"].strip():
            raise ValueError(f"An edit picture must name a file path, not {item.get('file')!r}")
    width = H3_IMAGE_EDIT_DEFAULT_WIDTH if width is None else width
    height = H3_IMAGE_EDIT_DEFAULT_HEIGHT if height is None else height
    for name, value in (("width", width), ("height", height)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 16:
            raise ValueError(f"An image edit {name} must be an integer of at least 16, not {value!r}")
    media_state = json.dumps(
        [
            {"kind": "picture", **item, "enabled": item.get("enabled", True)}
            for item in pictures
        ],
        separators=(",", ":"),
    )
    condition_inputs: dict[str, Any] = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "length": 5,
        "ref_image_size": "max",
        "clip": ["mvp:clip", 0],
        "vae": ["mvp:image_vae", 0],
        "audio_vae": ["mvp:audio_vae", 0],
        # The base picture arrives scaled; every further picture rides the splitter raw.
        "ref_images.ref_image_0": ["mvp:scale", 0],
    }
    for index in range(1, len(pictures)):
        condition_inputs[f"ref_images.ref_image_{index}"] = [
            "mvp:split", H3_SPLIT_OFFSETS["picture"] + index,
        ]
    model_source = ["mvp:model", 0] if sampling.lora is None else ["mvp:lora", 0]
    return {
        "mvp:model": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        **(
            {}
            if sampling.lora is None
            else {
                "mvp:lora": {
                    "class_type": "LoraLoaderModelOnly",
                    "inputs": {
                        "model": ["mvp:model", 0],
                        "lora_name": sampling.lora,
                        "strength_model": sampling.lora_strength,
                    },
                }
            }
        ),
        "mvp:attention": attention_profile.node(model_source),
        "mvp:clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "type": "minimax", "device": "default"}},
        "mvp:image_vae": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_t1_image_vae_step1597.safetensors"}},
        "mvp:audio_vae": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "mvp:references": {"class_type": "MiniMaxH3MediaLoader", "inputs": {"media_state": media_state}},
        "mvp:split": {"class_type": "MiniMaxH3ReferenceSplitter", "inputs": {"references": ["mvp:references", 0]}},
        "mvp:scale": {"class_type": "ImageScaleToTotalPixelsX", "inputs": {"image": ["mvp:split", H3_SPLIT_OFFSETS["picture"]], "megapixels": 2, "multiple_of": 32, "resize_mode": "crop", "upscale_method": "lanczos"}},
        "mvp:condition": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": condition_inputs},
        "mvp:latent": {"class_type": "EmptyMiniMaxH3ImageLatentAV", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "mvp:sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampling.sampler}},
        "mvp:scheduler": {"class_type": "BasicScheduler", "inputs": {"model": ["mvp:attention", 0], "scheduler": sampling.scheduler, "steps": sampling.steps if steps is None else steps, "denoise": 1.0}},
        "mvp:noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "mvp:guider": {"class_type": "BasicGuider", "inputs": {"model": ["mvp:attention", 0], "conditioning": ["mvp:condition", 0]}},
        "mvp:sample": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["mvp:noise", 0], "guider": ["mvp:guider", 0], "sampler": ["mvp:sampler", 0], "sigmas": ["mvp:scheduler", 0], "latent_image": ["mvp:latent", 0]}},
        "mvp:image": {"class_type": "VAEDecode", "inputs": {"samples": ["mvp:sample", 0], "vae": ["mvp:image_vae", 0]}},
        "mvp:save": {"class_type": "SaveImage", "inputs": {"images": ["mvp:image", 0], "filename_prefix": prefix}},
    }


# The LTX 2.5 video VAE's total spatial compression; see the normalizer node below
# for why 32 rather than 16. Kept as one constant so the floor applied here and the
# divisor handed to ImageResizeKJv2 cannot drift apart.
LTX25_DIVISOR = 32


def normalize_to_divisor(value: int, divisor: int = LTX25_DIVISOR) -> int:
    """Round ``value`` down to a multiple of ``divisor``, never below one cell.

    ``ImageResizeKJv2`` floors each axis to the divisor (``width -= width %
    divisor``), so any axis smaller than one cell becomes 0 — a zero-sized resize,
    not a small one. One divisor cell is the smallest size the LTX VAE boundary can
    actually accept, so that is the floor.
    """
    if divisor < 1:
        raise ValueError("divisor must be at least 1")
    if value < 1:
        raise ValueError("Image axes must be at least 1 pixel")
    return max(divisor, value - (value % divisor))


def patch_ltx25_dimension_boundary(
    template: dict[str, dict[str, Any]],
    *,
    source_size: tuple[int, int] | None = None,
) -> dict[str, dict[str, Any]]:
    """Patch the audited combined LTX 2.5 export without mutating its source.

    This is reference adaptation only; the full combined graph is not exposed
    for submission because it still contains creator-specific media paths and
    multiple expensive output branches.

    ``source_size`` is the SeedVR2 frame size in pixels when the caller knows it.
    Given it, both axes are normalized here through ``normalize_to_divisor``, so a
    frame with an axis under the divisor cannot floor to zero.
    Left as ``None`` the node keeps deriving the size from its input at runtime
    (``width``/``height`` 0), which is the export's own behaviour and carries that
    floor-to-zero risk for sub-divisor frames — pass the size to eliminate it.

    That risk is sharper under ``keep_proportion="crop"`` than it was under
    ``"resize"``. Verified by executing the installed node: a sub-divisor frame
    with derived size raises ``ZeroDivisionError`` in crop mode (it divides by the
    floored target height) where resize mode raised ``ValueError: height and width
    must be > 0``. Both are failures, but the crop-mode one is the more obscure,
    so passing ``source_size`` matters more now, not less.
    """
    payload = deepcopy(template)
    expected = {
        "6112": "easy cleanGpuUsed",
        "6116:6070": "VAEEncode",
        "6116:4970": "LTXVImgToVideoInplace",
        "6116:6073": "GetImageSize",
    }
    for node_id, class_type in expected.items():
        if payload.get(node_id, {}).get("class_type") != class_type:
            raise ValueError(f"Unsupported LTX 2.5 template: missing {node_id} {class_type}")
    width, height = (
        (0, 0)
        if source_size is None
        else (
            normalize_to_divisor(source_size[0], LTX25_DIVISOR),
            normalize_to_divisor(source_size[1], LTX25_DIVISOR),
        )
    )
    payload["mvp:ltx-size-normalize"] = {
        "class_type": "ImageResizeKJv2",
        "inputs": {
            "image": ["6112", 0],
            "width": width,
            "height": height,
            "upscale_method": "lanczos",
            # "crop", not "resize": the Director's ruling is that geometry is
            # preserved and a few trimmed pixels are the acceptable price.
            # "resize" resamples straight to the target (crop="disabled"), which
            # squashed 1250x720 into 1248x704 -- a 2.07% anamorphic stretch,
            # measured by executing the node. "crop" centre-crops to the target
            # aspect first (1250x705), then resamples 705 -> 704, leaving 0.02%
            # residual distortion and costing 15 rows of frame.
            "keep_proportion": "crop",
            "pad_color": "0, 0, 0",
            # Splits the trimmed rows between top and bottom (7/8 here) rather
            # than taking them all off one edge.
            "crop_position": "center",
            # The LTX 2.5 video VAE's total spatial compression is 32 (a 4-pixel
            # patchify followed by three stride-2 stages), and it sets
            # crop_input=False so nothing auto-corrects an odd size. 16 clears
            # patchify but leaves 720 mid-stack (720/32 = 22.5); 32 divides
            # exactly at every stage, taking 1250x720 to 1248x704.
            "divisible_by": LTX25_DIVISOR,
            "device": "cpu",
        },
    }
    normalized = ["mvp:ltx-size-normalize", 0]
    payload["6116:6070"]["inputs"]["pixels"] = normalized
    payload["6116:4970"]["inputs"]["image"] = normalized
    payload["6116:6073"]["inputs"]["image"] = normalized
    return payload


#: The sheet's one sampling pass, as node 53 of the creator's graph carries it.
#:
#: Named rather than inlined so the pre-flight can check the sampler against the live
#: server's option list, and so the test that pins these values against
#: ``krea2-charactersheet-user-export.json`` has one thing to compare.
#:
#: **This is one pass and it stays one pass.** ``Music-Video.md`` says the sheet is
#: "Three KSamplers: 10-step euler for the initial sheet, 8-step euler at CFG 0.3 for
#: the refine pass, 8-step res_multistep for the final output", and the graph it
#: describes does not support any part of that sentence:
#:
#: * The graph's three samplers are node 53 (``mode: 0``) and nodes 146 and 172, both
#:   ``mode: 4`` — bypassed on load. Only 53 is in the sheet path; the ``CharSheet-*``
#:   groups sit at positive x, while 146 and 172 live in a group titled ``2nd Sampling``
#:   at x ≈ -1129, feeding an optional *character portrait generator* whose output can
#:   become the sheet's input image.
#: * "CFG 0.3" is node 172's ``denoise``. KSampler's widget order is (seed, control,
#:   steps, cfg, sampler_name, scheduler, denoise); all three samplers carry cfg 1.0.
#: * ``res_multistep`` is node 146, the *first* of that pair, not the final pass.
#:
#: Adding those two passes after the sheet was tried on 2026-08-18 and reverted. They are
#: not a documented combination — they are a third combination nobody has rendered, and
#: the final pass's denoise had to be invented outright because no source specifies one.
#: Shipping it as the default would have changed every reference sheet in the project on
#: the strength of a prose error. ``test_the_sheet_is_one_pass_and_the_creator_graph_says_why``
#: pins this, and it is the test to read before "fixing" the missing passes.
MULTIVIEW_STEPS = 10
MULTIVIEW_SAMPLER = "euler"
MULTIVIEW_SCHEDULER = "simple"
MULTIVIEW_CFG = 1.0
MULTIVIEW_DENOISE = 1.0


def build_multiview_payload(
    *, image_name: str, prompt: str, seed: int, prefix: str
) -> dict[str, dict[str, Any]]:
    """Krea 2 identity edit using the installed QuadView multi-view LoRA.

    Node ids are the creator's: 182/123/148/152/127/149/150/73/135/120/119/85/53/54/29
    are copied from the ``CharSheet-*`` groups of
    ``workflow_templates/reference_exports/krea2-charactersheet-user-export.json``, which
    is exactly the reachable sheet path and nothing else. One sampling pass, because that
    is all the sheet path has — see ``MULTIVIEW_STEPS`` above for what the pipeline
    documentation claims instead and why it is wrong.

    Nothing here depends on how many views the sheet comes back with. The pass runs on the
    whole latent and the save writes the whole image, so a sheet of four panels and a sheet
    of six go through this graph unchanged — which is what a probe found the QuadView LoRA
    actually does when asked for four.
    """
    return {
        "182": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "123": {
            "class_type": "ImageResizeKJv2",
            "inputs": {
                "image": ["182", 0],
                "width": 1024,
                "height": 1024,
                "upscale_method": "lanczos",
                "keep_proportion": "resize",
                "pad_color": "0, 0, 0",
                "crop_position": "center",
                "divisible_by": 2,
                "device": "cpu",
            },
        },
        "148": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": "krea2_turbo_fp8_scaled.safetensors",
                "weight_dtype": "default",
            },
        },
        "152": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["148", 0],
                "lora_name": r"krea2\Krea2-realism-V2.safetensors",
                "strength_model": 1.0,
            },
        },
        "127": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["152", 0],
                "lora_name": r"krea2\QuadView_krea2_v1.safetensors",
                "strength_model": 1.0,
            },
        },
        "149": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": "qwen3vl_4b_fp8_scaled.safetensors",
                "type": "krea2",
                "device": "default",
            },
        },
        "150": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": "qwen_image_vae.safetensors"},
        },
        "73": {
            "class_type": "VAEEncode",
            "inputs": {"pixels": ["123", 0], "vae": ["150", 0]},
        },
        "135": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": 1536, "height": 1024, "batch_size": 1},
        },
        "120": {
            "class_type": "Krea2EditModelPatch",
            "inputs": {
                "model": ["127", 0],
                "source_latent": ["73", 0],
                "ref_boost": 1.0,
                "ref_boost_a": 1.0,
                "fit_mode": "fit",
                "vae": ["150", 0],
                "source_image": ["123", 0],
                "target_latent": ["135", 0],
            },
        },
        "119": {
            "class_type": "Krea2EditGroundedEncode",
            "inputs": {
                "clip": ["149", 0],
                "prompt": prompt,
                "image": ["123", 0],
                "grounding_px": 0,
                "system_prompt": "",
            },
        },
        "85": {
            "class_type": "Krea2EditGroundedEncode",
            "inputs": {
                "clip": ["149", 0],
                "prompt": "",
                "image": ["123", 0],
                "grounding_px": 768,
                "system_prompt": "",
            },
        },
        "53": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["120", 0],
                "seed": seed,
                "steps": MULTIVIEW_STEPS,
                "cfg": MULTIVIEW_CFG,
                "sampler_name": MULTIVIEW_SAMPLER,
                "scheduler": MULTIVIEW_SCHEDULER,
                "positive": ["119", 0],
                "negative": ["85", 0],
                "latent_image": ["135", 0],
                "denoise": MULTIVIEW_DENOISE,
            },
        },
        "54": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["53", 0], "vae": ["150", 0]},
        },
        "29": {
            "class_type": "SaveImage",
            "inputs": {"images": ["54", 0], "filename_prefix": prefix},
        },
    }


def reachable_node_ids(graph: dict[str, dict[str, Any]], roots: list[str]) -> set[str]:
    """Every node ``roots`` depend on, following ``[node, output]`` links backwards.

    The rule that produced ``build_ltx25_enhance_payload``, written as a function rather
    than left as a claim in a comment, so a test can check the adapter against it and the
    pre-flight can say what it audited.

    A saved ComfyUI graph is the *editor's* set of nodes, not the executed one. ComfyUI
    executes backwards from the output nodes, so a loader nothing downstream reads is never
    loaded and its model file is not a dependency at all — it is a leftover from whichever
    parent graph the export was cut out of. Building a dependency list from
    ``graph.values()`` therefore declares files the task never touches, and a pre-flight
    built on that list refuses to run on a machine that is perfectly capable of the work.

    ``roots`` are the ids the caller knows to be outputs. Passed in rather than detected,
    because "is an output node" is a property of the node *class* — ``OUTPUT_NODE`` in
    ComfyUI, which a payload does not carry — and guessing it from the graph would be the
    same kind of inference this function exists to replace.
    """
    missing = [node_id for node_id in roots if node_id not in graph]
    if missing:
        raise ValueError(f"Not in this graph: {', '.join(sorted(missing))}")
    reached: set[str] = set()
    frontier = list(roots)
    while frontier:
        node_id = frontier.pop()
        if node_id in reached:
            continue
        reached.add(node_id)
        for value in graph[node_id].get("inputs", {}).values():
            # The link shape ComfyUI's API format uses, and the only way one node names
            # another. A bare string input that happens to look like a node id is a value,
            # not an edge, so the length and the integer output index are both checked.
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and isinstance(value[1], int)
                and not isinstance(value[1], bool)
                and value[0] in graph
            ):
                frontier.append(value[0])
    return reached


#: The longest side ``ImageScaleToMaxDimension`` scales the source's frames to, from the
#: export's ``INTConstant`` (node ``1991``, titled "LARGEST RESOLUTION"). Reproduced as a
#: constant rather than exposed: it is the resolution the graph's evidence was produced at,
#: and this adapter's job is to reach that graph rather than to retune it.
#:
#: Despite the sibling ``LatentUpscaleModelLoader`` sitting in the export, **there is no
#: latent upscale in the executed path**. That loader is orphaned; the only resize here is
#: this lanczos pixel-space scale ahead of the VAE encode.
LTX25_ENHANCE_LARGEST_SIZE = 1920

#: The sampling the export fixes, reproduced whole and deliberately not offered as controls.
#: Exposing the sigmas, the detailer strength or the prompt is marked Ask First in the spec
#: and has not been asked, so each is a constant here: three sigma steps, cfg 1 through a
#: ``CFGGuider``, ``euler``, seed 0, and an **empty** prompt. Three steps at cfg 1 with an
#: IC-LoRA detailer is a refinement pass rather than a generation — which is the whole reason
#: the take being enhanced survives recognisably.
LTX25_ENHANCE_SIGMAS = "0.909375, 0.725, 0.421875, 0.0"
LTX25_ENHANCE_CFG = 1.0
LTX25_ENHANCE_SAMPLER = "euler"
LTX25_ENHANCE_SEED = 0
LTX25_ENHANCE_PROMPT = ""

#: The IC-LoRA that supplies the detail, and the strength the export applies it at.
LTX25_ENHANCE_DETAILER_LORA = "ltx-2-19b-ic-lora-detailer.safetensors"
LTX25_ENHANCE_DETAILER_STRENGTH = 0.9

#: The three models the reachable subgraph loads besides the LoRA above. Named here so the
#: payload can be built from them; the pre-flight still reads its file list *out of a built
#: payload* rather than from this block — see ``tests/preflight_ltx25_enhance.py``. The audio
#: VAE and the latent upscaler that also appear in the export are absent, and that is the
#: point: nothing reachable from the output loads either one.
LTX25_ENHANCE_UNET = "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors"
LTX25_ENHANCE_VIDEO_VAE = "ltx-2.5-video-vae-conv-bf16.safetensors"
LTX25_ENHANCE_CLIP = "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors"

#: The container extensions ``VHS_LoadVideoPath`` accepts, which the live schema publishes as
#: ``video[1]["vhs_path_extensions"]``. Not this adapter's policy — refused locally only so a
#: take in a container the node cannot open is named here instead of arriving as an opaque
#: rejection from ``/prompt`` validation. ``tests/preflight_ltx25_enhance.py`` checks this
#: tuple against the live schema rather than trusting it.
LTX25_ENHANCE_SOURCE_EXTENSIONS = ("webm", "mp4", "mkv", "gif", "mov")


def build_ltx25_enhance_payload(*, source_video: str, prefix: str) -> dict[str, dict[str, Any]]:
    """Enhance an existing video through the standalone LTX 2.5 graph.

    Derived from ``workflow_templates/reference_exports/ltx25-enhancer-user-export.json`` —
    20 nodes of which **18 are reachable** from its single ``VHS_VideoCombine``. This builds
    those 18 and no others. ``reachable_node_ids`` is the rule, and ``tests/test_workflows.py``
    checks this payload against the export through it rather than against a list retyped here.

    The two it does not build are ``VAELoaderKJ`` on ``ltx-2.5-audio-vae-bf16.safetensors``
    and ``LatentUpscaleModelLoader`` on
    ``ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors``. Both are orphans inherited
    from the parent graph the export was cut from, and declaring either as a dependency would
    make the pre-flight refuse a machine that can do this work. The second also misleads by
    name: **no latent upscale runs here**. The executed path is ``ImageScaleToMaxDimension``
    (lanczos, against ``LTX25_ENHANCE_LARGEST_SIZE``) → ``VAEEncodeTiled`` →
    ``LTXVLoopingSampler`` → ``LTXVSpatioTemporalTiledVAEDecode``, and the detail comes from
    the IC-LoRA.

    **Nothing here regenerates the take.** ``source_video`` is loaded and re-sampled; there is
    no MiniMax H3 node in this graph at all, and the only image entering the VAE encode is the
    source's own frames.

    **The audio is the source's**, straight from the loader's third output to the saver, with
    no audio VAE, no audio decode and no synthesis anywhere in the graph. A source with no
    audio track needs no special case: ``VHS_VideoCombine`` reads ``audio['waveform']`` inside
    a ``try``, and an absent track leaves it unset, so the saver writes video only. Nothing can
    invent a track because nothing here can produce one.

    **The output frame count is not claimed to equal the input's.** Two things in this path can
    change it and neither is predicted here: the loader's ``format="LTXV"`` conforms the loaded
    frames to the node's own grid, and ``LTXVLoopingSampler`` tiles temporally (tile 56, overlap
    24). The reference chain's LTX stage turned 192 frames into 185. Measure the result with
    ``ffprobe``; do not assume.

    Two node classes are substituted for schema-visibility, both stated because an unstated
    substitution is the part that would mislead:

    * ``VHS_LoadVideo`` → ``VHS_LoadVideoPath``. The export's loader takes a filename from a
      combo of ComfyUI's *input* directory, which live ``/object_info`` publishes empty on this
      installation. A rendered take lives under ComfyUI's **output** directory, so there is no
      filename that node could be given. ``VHS_LoadVideoPath`` is the same loader with a
      ``STRING`` path — identical outputs in identical order (``IMAGE``, ``frame_count``,
      ``audio``, ``video_info``), so the wiring is unchanged.
    * ``Power Lora Loader (rgthree)`` → ``LoraLoader``. Live ``/object_info`` declares *no*
      ``lora_*`` inputs on the rgthree loader — its rows are client-side widgets — so a
      faithful copy would put the filename and the strength where the pre-flight cannot see
      them, which is the reasoning ``build_h3_reference_payload`` already records for its own
      substitution. ``LoraLoader`` is used rather than that function's ``LoraLoaderModelOnly``
      because this graph draws **both** the model and the CLIP through the loader, and one
      enabled rgthree row with a single strength applies that strength to both. The cost is the
      same one stated there: this adapter is evidenced in its *values* and not in that node's
      wiring.
    """
    if not isinstance(source_video, str) or not source_video.strip():
        raise ValueError("An LTX 2.5 enhancement needs a source video path")
    # VHS strips surrounding whitespace and one leading/trailing quote off the path before
    # opening it (``utils.strip_path``). The file a caller checked on disk and the file the
    # node then opens must be the same one, so a value that would be rewritten is refused
    # rather than silently repointed.
    if source_video != source_video.strip().strip('"'):
        raise ValueError(
            f"The source video path must not be quoted or padded: VHS would rewrite "
            f"{source_video!r} before opening it"
        )
    extension = source_video.rsplit(".", 1)[-1].lower() if "." in source_video else ""
    if extension not in LTX25_ENHANCE_SOURCE_EXTENSIONS:
        raise ValueError(
            f"LTX 2.5 enhancement reads {', '.join(LTX25_ENHANCE_SOURCE_EXTENSIONS)}; "
            f"{source_video} is not one of those"
        )
    return {
        "mvp:source": {
            "class_type": "VHS_LoadVideoPath",
            "inputs": {
                "video": source_video,
                "force_rate": 0,
                "custom_width": 0,
                "custom_height": 0,
                "frame_load_cap": 0,
                "skip_first_frames": 0,
                "select_every_nth": 1,
                "format": "LTXV",
            },
        },
        # The source's own timing, read back off the loader. Both the saver's frame rate and
        # the LTX conditioning's take it from here, exactly as the export does, so a 24 fps
        # take is enhanced and written at 24 fps without this adapter guessing a number.
        "mvp:source_info": {
            "class_type": "VHS_VideoInfo",
            "inputs": {"video_info": ["mvp:source", 3]},
        },
        "mvp:largest_size": {
            "class_type": "INTConstant",
            "inputs": {"value": LTX25_ENHANCE_LARGEST_SIZE},
        },
        "mvp:scale": {
            "class_type": "ImageScaleToMaxDimension",
            "inputs": {
                "upscale_method": "lanczos",
                "largest_size": ["mvp:largest_size", 0],
                "image": ["mvp:source", 0],
            },
        },
        "mvp:model": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": LTX25_ENHANCE_UNET, "weight_dtype": "default"},
        },
        "mvp:clip": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": LTX25_ENHANCE_CLIP, "type": "ltxv", "device": "default"},
        },
        "mvp:video_vae": {
            "class_type": "VAELoaderKJ",
            "inputs": {
                "vae_name": LTX25_ENHANCE_VIDEO_VAE,
                "device": "main_device",
                "weight_dtype": "bf16",
            },
        },
        "mvp:detailer": {
            "class_type": "LoraLoader",
            "inputs": {
                "model": ["mvp:model", 0],
                "clip": ["mvp:clip", 0],
                "lora_name": LTX25_ENHANCE_DETAILER_LORA,
                "strength_model": LTX25_ENHANCE_DETAILER_STRENGTH,
                "strength_clip": LTX25_ENHANCE_DETAILER_STRENGTH,
            },
        },
        "mvp:prompt": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": LTX25_ENHANCE_PROMPT, "clip": ["mvp:detailer", 1]},
        },
        # One encode feeds both sides, as the export does: at cfg 1 the guider never evaluates
        # a separate negative branch, so a second empty encode would cost time and change
        # nothing.
        "mvp:conditioning": {
            "class_type": "LTXVConditioning",
            "inputs": {
                "frame_rate": ["mvp:source_info", 0],
                "positive": ["mvp:prompt", 0],
                "negative": ["mvp:prompt", 0],
            },
        },
        "mvp:guider": {
            "class_type": "CFGGuider",
            "inputs": {
                "cfg": LTX25_ENHANCE_CFG,
                "model": ["mvp:detailer", 0],
                "positive": ["mvp:conditioning", 0],
                "negative": ["mvp:conditioning", 1],
            },
        },
        "mvp:sigmas": {
            "class_type": "ManualSigmas",
            "inputs": {"sigmas": LTX25_ENHANCE_SIGMAS},
        },
        "mvp:sampler": {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": LTX25_ENHANCE_SAMPLER},
        },
        "mvp:noise": {
            "class_type": "RandomNoise",
            "inputs": {"noise_seed": LTX25_ENHANCE_SEED},
        },
        "mvp:encode": {
            "class_type": "VAEEncodeTiled",
            "inputs": {
                "tile_size": 512,
                "overlap": 64,
                "temporal_size": 500,
                "temporal_overlap": 4,
                "pixels": ["mvp:scale", 0],
                "vae": ["mvp:video_vae", 0],
            },
        },
        # The encoded source is fed twice on purpose: as the latents being sampled and as
        # ``optional_guiding_latents``, which is what makes this an enhancement *of that take*
        # rather than a generation that happens to start near it. Guiding latents are the
        # node's own documented pairing with an IC-LoRA.
        "mvp:sample": {
            "class_type": "LTXVLoopingSampler",
            "inputs": {
                "temporal_tile_size": 56,
                "temporal_overlap": 24,
                "guiding_strength": 1,
                "temporal_overlap_cond_strength": 0.5,
                "cond_image_strength": 1,
                "horizontal_tiles": 1,
                "vertical_tiles": 1,
                "spatial_overlap": 1,
                "adain_factor": 0,
                "guiding_start_step": 0,
                "guiding_end_step": 1000,
                "optional_cond_image_indices": "0",
                "model": ["mvp:detailer", 0],
                "vae": ["mvp:video_vae", 0],
                "noise": ["mvp:noise", 0],
                "sampler": ["mvp:sampler", 0],
                "sigmas": ["mvp:sigmas", 0],
                "guider": ["mvp:guider", 0],
                "latents": ["mvp:encode", 0],
                "optional_guiding_latents": ["mvp:encode", 0],
            },
        },
        "mvp:decode": {
            "class_type": "LTXVSpatioTemporalTiledVAEDecode",
            "inputs": {
                "spatial_tiles": 4,
                "spatial_overlap": 4,
                "temporal_tile_length": 48,
                "temporal_overlap": 8,
                "last_frame_fix": False,
                "working_device": "auto",
                "working_dtype": "auto",
                "vae": ["mvp:video_vae", 0],
                "latents": ["mvp:sample", 0],
            },
        },
        "mvp:save": {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "frame_rate": ["mvp:source_info", 0],
                "loop_count": 0,
                "filename_prefix": prefix,
                "format": "video/h264-mp4",
                "pix_fmt": "yuv420p",
                "crf": 19,
                "save_metadata": True,
                "trim_to_audio": False,
                "pingpong": False,
                "save_output": True,
                "images": ["mvp:decode", 0],
                # The source's audio, carried through untouched. This is the *only* audio in
                # the graph: there is no audio VAE and no audio decode anywhere in it.
                "audio": ["mvp:source", 2],
            },
        },
    }


#: The container extensions each loader on the audio-replacement path accepts, published by
#: live ``/object_info`` as ``video[1]["vhs_path_extensions"]`` and
#: ``audio_file[1]["vhs_path_extensions"]``. Restated locally only so a take or a song in a
#: container the node cannot open is named here instead of arriving as an opaque ``/prompt``
#: rejection; ``tests/preflight_audio_replace.py`` checks both tuples against the live schema
#: rather than trusting them. The video list is the same one
#: ``LTX25_ENHANCE_SOURCE_EXTENSIONS`` carries, and is deliberately *not* an alias of it: the
#: two adapters restate the same node's list for their own reasons, and aliasing would make a
#: divergence in one of them invisible in the other's audit.
AUDIO_REPLACE_VIDEO_EXTENSIONS = ("webm", "mp4", "mkv", "gif", "mov")
AUDIO_REPLACE_AUDIO_EXTENSIONS = ("wav", "mp3", "ogg", "m4a", "flac")

#: What ``VHS_LoadVideoPath.format`` is set to, and the single most consequential departure
#: from the audited export.
#:
#: The export loads its video with ``format: "LTXV"``, inherited from the LTX graph it was cut
#: out of. Live ``/object_info`` publishes what that string means: ``{"target_rate": 24,
#: "dim": [32, 0, 768, 512], "frames": [8, 1]}`` — force the rate to 24, floor both axes to a
#: multiple of 32 with a 768x512 minimum, and conform the frame count to ``8n+1``. Every one
#: of those would change the picture this stage exists to copy through, and the frame rule is
#: the dangerous one: an H3 take lands on the **17k+5** grid, so a 90-frame take would be cut
#: to 89 and the restored song would run one frame long against a picture one frame short.
#: That is precisely the silent desync this whole stage is shaped to avoid.
#:
#: ``"None"`` publishes an empty spec — no target rate, no dimension floor, no frame rule — so
#: the frames reach the saver as the take decoded them.
AUDIO_REPLACE_SOURCE_FORMAT = "None"

#: The output container, reproduced from the export's ``VHS_VideoCombine``.
#:
#: **Stated plainly because the word "copy" would overclaim it:** this is a re-encode. ComfyUI
#: has no stream-copy mux; ``VHS_LoadVideoPath`` decodes the take to IMAGE tensors and
#: ``VHS_VideoCombine`` encodes them again at h264 crf 19. What the frozen spec's "the picture
#: is copied through, never re-encoded through a generative model" rules out is a *generative*
#: pass, and there is none here — no UNET, no VAE, no sampler, no model file of any kind in
#: this payload. What the Director gets is the take's own frames through one more h264
#: generation at the same crf the take itself was written at. If a bit-identical video stream
#: is ever required, that is an ffmpeg ``-c:v copy`` mux and not a ComfyUI graph at all.
AUDIO_REPLACE_FORMAT = "video/h264-mp4"
AUDIO_REPLACE_PIX_FMT = "yuv420p"
AUDIO_REPLACE_CRF = 19


def build_audio_replace_payload(
    *,
    source_video: str,
    source_audio: str,
    start: float,
    duration: float,
    song_duration: float,
    take_lead: float,
    prefix: str,
) -> dict[str, dict[str, Any]]:
    """Put the master song's own seconds back over a finished take.

    Derived from ``workflow_templates/reference_exports/ltx25-audioreplacer-user-export.json``
    — 8 nodes of which **only 3 are reachable** from its single ``VHS_VideoCombine``:
    ``LoadAudio``, ``VHS_LoadVideo`` and the saver. The other five are a ``UNETLoader``, two
    ``VAELoaderKJ``, a ``CLIPLoader`` and a ``LatentUpscaleModelLoader``, inherited from the
    parent LTX graph the export was cut out of. This builder builds the three and no others,
    and the arithmetic that matters is what falls out of that: **this payload names zero model
    files.** Nothing here loads a checkpoint, a VAE, a CLIP or a LoRA, because nothing here
    generates anything. ``reachable_node_ids`` is the rule and ``tests/test_workflows.py``
    checks this payload against the export through it rather than against a list retyped here.

    **The window is not computed here and cannot be.** ``start``, ``duration``,
    ``song_duration`` and ``take_lead`` are the four numbers the *render* was given or recorded,
    and every one of them is handed straight to a function ``generate_h3`` also calls. There is
    deliberately no window parameter: a caller cannot pass a window this builder would then
    trust, so there is no second computation of "which seconds is this take" anywhere on this
    path and no way for the two to drift.

    ``start`` and ``duration`` are **the take's window, not the shot's live one**, and this
    builder cannot check that — the two are the same numbers and only the caller knows which
    pair it read. `app.restore_song_audio` passes `Shot.latest_take_start`/
    `latest_take_duration`, the snapshot written beside the lead at submission, and falls back
    to the live window only for a take that recorded none, reporting that it did. The
    distinction is the 2026-08-21 finding: a window edited after a render leaves this builder
    computing a correct payload for a take that does not exist.

    Two functions, because there are two questions and they have different answers:

    * ``song_audio_window`` answers *is this shot legal at all* — the past-the-end refusal,
      inherited rather than restated, so the two stages refuse the same shot for the same reason
      in the same words. Its window is the **exposed slice**, and that is what it is used for;
    * ``timeline.over_render_window`` answers *which seconds is the take*, which is the window
      this graph actually carries. Since the over-render margin (2026-08-19) and the micro-cut
      floor (2026-08-20), a take is not its window: it is `over_render_frames(duration)` frames
      of picture beginning ``take_lead`` seconds before the window. Windowing this stage by
      ``start``/``duration`` — which is what it did until 2026-08-21 — laid the exposed slice's
      seconds over the *whole* take, so the sound ran ``take_lead`` seconds ahead of the mouth
      and stopped a margin early. On a centred micro-cut that is a 2.083 s slice over 4.4583 s
      of picture, offset by 1.2083 s.

    ``take_lead`` is `Shot.latest_take_lead`, **recorded at submission and never recomputed**: a
    pre-margin take and a post-margin one are indistinguishable by arithmetic on their lengths,
    which is the whole reason that field exists. This builder therefore refuses a lead it can
    prove is not this shot's (negative, or longer than the shot's own start) rather than
    repairing it into a plausible window. Deciding what a *missing* lead means is the caller's:
    see ``app.restore_song_audio``.

    The window reaches ComfyUI as ``VHS_LoadAudio``'s ``seek_seconds`` and ``duration``, which
    is the node slicing the file as it reads it. Nothing downstream trims: ``trim_to_audio`` is
    ``False``, so a picture longer than its audio is left long rather than cut, and a picture
    shorter is left short rather than padded. Neither is silently corrected — the two lengths
    are reported to the caller instead.

    Three node classes are involved and two of them are substitutions, both stated because an
    unstated substitution is the part that would mislead:

    * ``VHS_LoadVideo`` -> ``VHS_LoadVideoPath``, for exactly the reason
      ``build_ltx25_enhance_payload`` records and re-verified against live ``/object_info`` on
      2026-08-18: that node's ``video`` combo enumerates ComfyUI's *input* directory, which
      this installation publishes **empty**, while a take lives under *output*. Same outputs in
      the same order (``IMAGE``, ``frame_count``, ``audio``, ``video_info``), so the wiring is
      unchanged.
    * ``LoadAudio`` -> ``VHS_LoadAudio``. The mirror of the same trap, and it is real here
      rather than assumed: live ``/object_info`` publishes ``LoadAudio.audio`` as a COMBO whose
      only option is the one file sitting in ComfyUI's input directory. The master song lives
      under this application's data root — a different drive — so there is no value that node
      could be given. ``VHS_LoadAudio`` ("Load Audio (Path)") takes a ``STRING`` path, and it
      is not merely a workaround: it is the only one of the two that can express a window at
      all, through the optional ``seek_seconds`` and ``duration`` this stage exists to set.
      Its first output is ``AUDIO``, which is what the saver reads.

    One node is *added* rather than substituted, and it is the fourth in the payload:
    ``VHS_VideoInfo``. The export hardcodes ``frame_rate: 25`` on its saver, which is the
    creator's own footage and not this application's — an H3 take is 24 fps, and writing its
    frames out at 25 would play the picture 4% fast against a soundtrack that is not. Reading
    the rate off the source is ``build_ltx25_enhance_payload``'s existing answer to the same
    problem, and it keeps this adapter from inventing a number about a file it can measure.

    **The take is not an input to anything that writes.** ``prefix`` is the caller's, and the
    only guarantee this builder can make about it is that it refuses an empty one; keeping the
    restored file out of the take's numbered series is the route's job and is checked there.
    """
    if not isinstance(source_video, str) or not source_video.strip():
        raise ValueError("Restoring a shot's song audio needs a source video path")
    if not isinstance(source_audio, str) or not source_audio.strip():
        raise ValueError("Restoring a shot's song audio needs the master song's path")
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError("Restoring a shot's song audio needs an output filename prefix")
    # VHS strips surrounding whitespace and one leading/trailing quote off a path before
    # opening it (``utils.strip_path``), on both loaders. The file a caller checked on disk and
    # the file the node then opens must be the same one, so a value that would be rewritten is
    # refused rather than silently repointed. Both paths, because both go through it.
    for label, value in (("source video", source_video), ("song", source_audio)):
        if value != value.strip().strip('"'):
            raise ValueError(
                f"The {label} path must not be quoted or padded: VHS would rewrite "
                f"{value!r} before opening it"
            )
    for label, value, accepted in (
        ("A take", source_video, AUDIO_REPLACE_VIDEO_EXTENSIONS),
        ("A master song", source_audio, AUDIO_REPLACE_AUDIO_EXTENSIONS),
    ):
        extension = value.rsplit(".", 1)[-1].lower() if "." in value else ""
        if extension not in accepted:
            raise ValueError(
                f"{label} must be one of {', '.join(accepted)}; {value} is not one of those"
            )
    # The whole correctness argument of this stage, in two calls. See the docstring: there is no
    # window parameter for a caller to disagree with, and neither the past-the-end refusal nor
    # the take's own arithmetic is a second copy written here.
    #
    # First the shot's legality, in the render's own words. Its answer is deliberately not the
    # window sent: this is the *exposed slice*, and what this graph lays audio over is the whole
    # take.
    song_audio_window(start=start, duration=duration, song_duration=song_duration)
    _finite("Take lead", take_lead)
    if take_lead < 0 or take_lead > start:
        raise ValueError(
            f"A take of a shot at {start:g}s cannot begin {take_lead:g}s before it — that lead "
            f"does not describe this shot, and guessing one would put the picture out of sync "
            f"with the sound that produced it."
        )
    # Then the take's own seconds. `over_render_frames` is the render's frame count, so the
    # audio is exactly as long as the picture the render asked H3 for, and `over_render_window`
    # is the offset rule `generate_h3` conditions with.
    trim_start, trim_end = over_render_window(
        start=start,
        lead=take_lead,
        picture_seconds=over_render_frames(duration) / H3_FRAME_RATE,
        song_duration=song_duration,
    )
    return {
        "mvp:source": {
            "class_type": "VHS_LoadVideoPath",
            "inputs": {
                "video": source_video,
                "force_rate": 0,
                "custom_width": 0,
                "custom_height": 0,
                "frame_load_cap": 0,
                "skip_first_frames": 0,
                "select_every_nth": 1,
                # Not the export's "LTXV". See `AUDIO_REPLACE_SOURCE_FORMAT`.
                "format": AUDIO_REPLACE_SOURCE_FORMAT,
            },
        },
        # The take's own frame rate, read back off the loader rather than hardcoded as the
        # export hardcodes 25.
        "mvp:source_info": {
            "class_type": "VHS_VideoInfo",
            "inputs": {"video_info": ["mvp:source", 3]},
        },
        # The master, sliced as it is read. `seek_seconds` and `duration` are the node's own
        # optional inputs and are the only place this window is expressed.
        "mvp:song": {
            "class_type": "VHS_LoadAudio",
            "inputs": {
                "audio_file": source_audio,
                "seek_seconds": trim_start,
                "duration": trim_end - trim_start,
            },
        },
        "mvp:save": {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "frame_rate": ["mvp:source_info", 0],
                "loop_count": 0,
                "filename_prefix": prefix,
                "format": AUDIO_REPLACE_FORMAT,
                "pix_fmt": AUDIO_REPLACE_PIX_FMT,
                "crf": AUDIO_REPLACE_CRF,
                "save_metadata": True,
                # Never silently cut the picture to the audio's length. A mismatch is reported
                # to the caller with both numbers instead; see `audio_replace_lengths`.
                "trim_to_audio": False,
                "pingpong": False,
                "save_output": True,
                # The take's frames, untouched by anything generative.
                "images": ["mvp:source", 0],
                # The master's own seconds. The take's *generated* audio — `mvp:source`
                # output 2 — is deliberately not wired anywhere: replacing it is the point,
                # and it stays recoverable in the take itself, which this graph only reads.
                "audio": ["mvp:song", 0],
            },
        },
    }


def audio_replace_lengths(
    *, start: float, duration: float, song_duration: float, take_lead: float
) -> dict[str, float]:
    """How many seconds of picture a shot's take holds against the seconds of song it gets.

    The matrix's length-mismatch row, answered with the two numbers that are actually known
    before anything is submitted, and named so neither is mistaken for the other:

    * ``audio_seconds`` is the length of the window this stage actually sends — the take's own
      seconds, from `timeline.over_render_window`, which is the same call
      `build_audio_replace_payload` fills the loader from;
    * ``requested_picture_seconds`` is the H3 **request**, not a measurement: the render asked
      for `timeline.over_render_frames` frames at 24 fps. A 3.75 s shot asks for 107 frames,
      which is 4.4583 s; a 2.083 s micro-cut asks for 107 as well, because every take is floored
      at H3's own four-second minimum.

    **Both numbers moved on 2026-08-21 and the pair is the point.** ``requested_picture_seconds``
    was still ``align_h3_frames(max(5, round(duration * 24)))`` — the formula from before the
    over-render margin existed — and under-reported a 2.083 s shot's take as 50 frames when the
    render asked for 107. ``audio_seconds`` was the shot's bare ``duration``, which was the same
    defect on the other side: the audio this stage sends covers the whole take, not the exposed
    slice. Reported together they now agree by construction, and the *remaining* way they can
    differ is the honest one — the song ending before the picture does, where
    ``over_render_window`` clamps the tail and the take keeps its unbacked frames rather than
    being cut.

    The measured/requested distinction is load-bearing and is the matrix's "frame count is
    measured, never asserted equal to the input": what the render *asked for* is knowable from
    the shot; what the take *contains* is an ``ffprobe`` reading of a file. This returns the
    first and never claims the second. The saver is told ``trim_to_audio: False``, so whichever
    is longer stays longer.

    Every number here comes from the `timeline` functions the submission path renders through,
    never from a copy of the grid, so what the Director is told a take is cannot disagree with
    what was asked for.

    **Which window it is told about is the caller's to get right.** ``requested_picture_seconds``
    is what a render of ``start``/``duration`` asks H3 for, and that is the count the submission
    sent only if those are the numbers the submission was given. `app.restore_song_audio` passes
    the take's recorded window for exactly this reason, and reports ``describes_take: false``
    when the take has none to pass — see `Shot.latest_take_start`. Nothing here can tell the two
    apart, so nothing here claims to.
    """
    _finite("Shot duration", duration)
    if duration <= 0:
        raise ValueError(f"A shot must last longer than zero seconds, not {duration:g}s")
    frames = over_render_frames(duration)
    trim_start, trim_end = over_render_window(
        start=start,
        lead=take_lead,
        picture_seconds=frames / H3_FRAME_RATE,
        song_duration=song_duration,
    )
    return {
        "audio_seconds": trim_end - trim_start,
        "requested_picture_seconds": frames / H3_FRAME_RATE,
        "requested_frames": float(frames),
    }


#: The models the LTX 2.5 video-extension graph's **reachable** subgraph loads. Five files,
#: and the fifth is the difference from every other LTX adapter here:
#: ``LatentUpscaleModelLoader`` is an *orphan* in the enhancer and the audio-replacer exports
#: and this one builds neither of those, but in
#: ``workflow_templates/reference_exports/ltx25-videoextender-user-export.json`` that loader
#: is genuinely reached — node ``16`` feeds ``LTXVLatentUpsampler`` (node ``1993:1921``),
#: which is the whole low-res-then-upscale shape of this graph. Declared here because the
#: reachability walk says so, not because the class name appeared in a node list; the same
#: walk is what keeps the other two adapters from declaring it.
LTX25_EXTEND_UNET = "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors"
LTX25_EXTEND_VIDEO_VAE = "ltx-2.5-video-vae-conv-bf16.safetensors"
LTX25_EXTEND_AUDIO_VAE = "ltx-2.5-audio-vae-bf16.safetensors"
LTX25_EXTEND_CLIP = "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors"
LTX25_EXTEND_UPSCALER = "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"

#: The two sampling schedules the audited export fixes, reproduced verbatim — spacing and
#: all — so the test that pins them against the export compares strings rather than parsed
#: floats.
#:
#: **Read these as a property of the graph, not as a defect.** The base pass starts at
#: **1.0**: full noise, eight steps, which is what generating seconds of video that do not
#: exist yet requires. The refine pass, after the latent upscale, starts at **0.85** — for
#: comparison ``LTX25_ENHANCE_SIGMAS`` starts at 0.909375, and *that* was measured to move
#: lip position on a singing take. So this graph re-generates at least as hard as the
#: enhancer does, and it does so over the reference tail as well as over the new seconds:
#: nothing splices the source's last frames back in, they come back out of the model (see
#: the ``mvp:head`` / ``mvp:generated`` split in ``build_ltx25_extend_payload``).
#:
#: The consequence, stated once here so nobody has to rediscover it: an extension suits a
#: long b-roll cut — an establishing shot, an environment, a texture — and would visibly
#: disturb a performance shot, because the seam second is re-generated and the new seconds
#: are invented outright. That is a judgement for whoever asks for the extension. It is
#: deliberately **not** encoded as a refusal, and no machinery here tries to protect
#: lip-sync.
LTX25_EXTEND_BASE_SIGMAS = "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"
LTX25_EXTEND_REFINE_SIGMAS = "0.85, 0.7250, 0.4219, 0.0"

#: The rest of the sampling the export fixes and this adapter does not offer as controls.
#: ``cfg`` 1 through a ``CFGGuider`` and ``euler`` are the export's; the refine pass's seed is
#: the export's second ``RandomNoise`` (node ``1993:1947``) and stays a constant because
#: re-tuning it is not evidenced, while the base pass's seed (node ``1999``) is the one that
#: decides what gets invented and is therefore the caller's.
LTX25_EXTEND_CFG = 1.0
LTX25_EXTEND_SAMPLER = "euler"
LTX25_EXTEND_REFINE_SEED = 28
LTX25_EXTEND_DEFAULT_SEED = 62189421302031

#: The export's own negative prompt (node ``1993:1936``), carried whole. Reproduced rather
#: than rewritten: it is part of the configuration the evidence was produced with, double
#: space and trailing comma included, and ``tests/test_workflows.py`` compares this string to
#: the export's byte for byte.
LTX25_EXTEND_NEGATIVE_PROMPT = (
    "text, subtitles, logo, still image, still video, no motion, static, frozen, blurry, "
    "low quality, distorted, bad anatomy, oversaturated, pixelated, low resolution, grainy, "
    "compression artifacts, jpeg artifacts, glitches, watermark, signature, copyright,  "
    "distortedsound, saturated sound, loud sound , deformed facial features, asymmetrical "
    "face, missing facial features, extra limbs, disfigured hands, blurry teeth, disfigured "
    "teeth, "
)

#: The export's positive prompt is empty (node ``1992``). An extension of an existing shot has
#: the shot itself as its conditioning; a caller with something to say about the new seconds
#: passes it, and a caller with nothing to say sends what the evidence sent.
LTX25_EXTEND_PROMPT = ""

#: The knobs the export exposes as titled ``INTConstant`` nodes, with their values as the
#: defaults: "Reference (in seconds) leave it at 3" (node ``2000``), "Framerate" (``1997``),
#: "WIDTH" (``1995``) and "HEIGHT" (``1996``). "Extend video by (seconds)" (``2001``, value
#: 10) is a required argument instead — there is no sensible default for how much video to
#: invent.
LTX25_EXTEND_DEFAULT_REFERENCE_SECONDS = 3
LTX25_EXTEND_DEFAULT_FRAME_RATE = 24
LTX25_EXTEND_DEFAULT_WIDTH = 1920
LTX25_EXTEND_DEFAULT_HEIGHT = 1080

#: Fixed values from the export that are neither models nor sampling: the longer edge the
#: anchor frame is scaled to before ``LTXVImgToVideoInplace`` (node ``1993:1942``), the
#: ``LTXVPreprocess`` compression (``1993:1932``), the loudness the source track is normalised
#: to (``1993:1938``), and the half-scale the reference tail is encoded at (``1993:1933``).
LTX25_EXTEND_ANCHOR_LONGER_EDGE = 1536
LTX25_EXTEND_IMG_COMPRESSION = 33
LTX25_EXTEND_LOUDNESS_LUFS = -16.0
LTX25_EXTEND_REFERENCE_SCALE = 0.5

#: The container extensions ``VHS_LoadVideoPath`` accepts, published by live ``/object_info``
#: as ``video[1]["vhs_path_extensions"]``. The same list ``LTX25_ENHANCE_SOURCE_EXTENSIONS``
#: and ``AUDIO_REPLACE_VIDEO_EXTENSIONS`` carry, and deliberately not an alias of either, for
#: the reason those two already record: three adapters restate one node's list for their own
#: reasons, and aliasing would make a divergence in one invisible in the others' audits.
#: ``tests/preflight_ltx25_extend.py`` checks this tuple against the live schema.
LTX25_EXTEND_SOURCE_EXTENSIONS = ("webm", "mp4", "mkv", "gif", "mov")

#: Every schema ceiling a *caller's argument* can push a literal past, restated so the refusal
#: happens here with the number named rather than arriving as an opaque 502 from ``/prompt``
#: validation after the submission round-trip. Each is the declared bound of the input the
#: value lands in, and ``tests/preflight_ltx25_extend.py`` reads all five back off live
#: ``/object_info`` rather than trusting this block:
#:
#: * ``FRAME_RATE`` — ``VHS_LoadVideoPath.force_rate`` max, and the rate reaches three more
#:   inputs with looser ceilings, so this is the binding one;
#: * ``RANGE_FRAMES`` — ``GetImageRangeFromBatch.num_frames`` max, which the reference tail's
#:   literal frame count has to sit inside;
#: * ``MASK_SECONDS`` — ``LTXVAudioVideoMask.video_end_time`` max, the ceiling on
#:   reference + extension;
#: * ``DIMENSION`` — ``ImageResizeKJv2.width``/``height`` max;
#: * ``SEED`` — ``RandomNoise.noise_seed`` max.
LTX25_EXTEND_MAX_FRAME_RATE = 60
LTX25_EXTEND_MAX_RANGE_FRAMES = 4096
LTX25_EXTEND_MAX_MASK_SECONDS = 10000.0
LTX25_EXTEND_MAX_DIMENSION = 16384
LTX25_EXTEND_MAX_SEED = 18446744073709551615


def _positive_integer(name: str, value: object) -> int:
    """An ``int`` that is not a ``bool`` and is at least 1, or a refusal naming it."""
    if isinstance(value, bool) or not isinstance(value, int):
        # ValueError rather than TypeError, as everywhere else in this file: every caller of
        # every builder here treats "this argument is unusable" as one condition and turns it
        # into one message for the Director. Splitting it by Python type would put the same
        # refusal on two paths for no reader's benefit.
        raise ValueError(  # noqa: TRY004
            f"{name} must be a whole number of frames or seconds, not {value!r}"
        )
    if value < 1:
        raise ValueError(f"{name} must be at least 1, not {value}")
    return value


def build_ltx25_extend_payload(
    *,
    source_video: str,
    prefix: str,
    extend_seconds: float,
    reference_seconds: int = LTX25_EXTEND_DEFAULT_REFERENCE_SECONDS,
    prompt: str = LTX25_EXTEND_PROMPT,
    seed: int = LTX25_EXTEND_DEFAULT_SEED,
    frame_rate: int = LTX25_EXTEND_DEFAULT_FRAME_RATE,
    width: int = LTX25_EXTEND_DEFAULT_WIDTH,
    height: int = LTX25_EXTEND_DEFAULT_HEIGHT,
    include_audio: bool = True,
) -> dict[str, dict[str, Any]]:
    """Continue an existing video past its own end, through the LTX 2.5 extension graph.

    Derived from ``workflow_templates/reference_exports/ltx25-videoextender-user-export.json``
    — 61 nodes, **all 61 of them reachable** from its single ``VHS_VideoCombine`` (node
    ``1994``). That is unusual here and it is why this export is the one reproduced: the
    sibling ``ltx25-videoextender-noaudio-user-export.json`` carries 65 nodes of which only 54
    are reachable, and the 11 orphans are its abandoned audio tail — an edit in progress rather
    than a finished graph. It also mis-wires ``LTXVEmptyLatentAudio.frame_rate`` to the "Extend
    video by (seconds)" constant instead of the framerate one, on both instances, which leaves
    no honest way to build from it: transcribing reproduces the defect and correcting it
    invents a graph nobody rendered. It saves nothing either — the silent path still loads the
    same five model files, audio VAE included, and still runs an audio branch, just fed with
    silence. ``docs/WORKFLOW-MAP.md`` records it as available and audited-second.

    **What this graph actually does**, because "extend" undersells it:

    * ``VHS_LoadVideoPath`` reads the take at a forced ``frame_rate`` and every frame is
      stretched to ``width`` x ``height`` (``mvp:frames``);
    * the batch is cut in two — a *head*, everything but the last ``reference_seconds``, and a
      *tail* of ``reference_seconds * frame_rate + 1`` frames;
    * the tail, at half scale, is what the model is conditioned on. ``LTXVAudioVideoMask``
      marks the seconds after it as the region to invent, ``max_length="pad"`` grows the latent
      to hold them, and eight steps from sigma 1.0 fill them in at low resolution;
    * ``LTXVLatentUpsampler`` doubles the result, the tail's first frame is re-anchored into it
      by ``LTXVImgToVideoInplace``, and three more steps from sigma 0.85 refine the whole thing;
    * the output is the untouched **head** followed by the model's rendering of the **tail plus
      the new seconds**.

    That last point is the one to carry away: the reference tail is not spliced back, it is
    re-generated. See ``LTX25_EXTEND_BASE_SIGMAS`` for how hard, and for why this is a
    capability for long b-roll and establishing shots rather than for a performance take. It is
    a note for the caller's judgement; nothing here refuses a shot on those grounds.

    **The output length is a measurement, not a prediction.** ``format="LTXV"`` conforms the
    loaded frame count to the node's ``8n+1`` grid before anything else happens, the mask pads
    to a latent boundary rather than to an exact second, and a fractional ``extend_seconds`` has
    no exact frame count to land on. What comes back is longer than what went in; how much
    longer is an ``ffprobe`` reading of the file. Do not assume.

    **The take must carry an audio track.** This graph encodes the tail of the source's own
    audio as conditioning even when ``include_audio`` is False, so a silent source has nothing
    to feed ``LTXVAudioVAEEncode``. It is not refused here — this builder cannot open the file —
    but it is the one precondition a caller has to check.

    ``include_audio`` decides only what reaches the **saver**. True (the export's shape) writes
    the source's own track, loudness-normalised, with the model's invented continuation concat-
    enated after it; the invented seconds are the model's guess at what the audio does next and
    are not the source of anything. False drops those three nodes and the saver's optional
    ``audio`` link, and writes picture only — which is what a cut destined for a separate sound
    pass wants. It does **not** reproduce the no-audio export, which additionally replaces the
    audio *conditioning* with an empty latent; saying otherwise would be the unstated
    substitution this file's other docstrings exist to avoid.

    Four departures from a node-for-node transcription, each stated because an unstated
    substitution is the part that would mislead:

    * ``VHS_LoadVideo`` -> ``VHS_LoadVideoPath``, for the reason
      ``build_ltx25_enhance_payload`` and ``build_audio_replace_payload`` both record: the
      export's loader takes a filename from a combo over ComfyUI's *input* directory, which
      this installation publishes empty, while a take lives under *output*. Identical outputs
      in identical order (``IMAGE``, ``frame_count``, ``audio``, ``video_info``).
    * ``Power Lora Loader (rgthree)`` (node ``1988``) is **dropped rather than substituted.**
      The enhancer had to substitute a ``LoraLoader`` because its rgthree row carried an
      enabled detailer; this export's node carries no ``lora_*`` entry at all, so it applies
      nothing and is a pass-through from ``UNETLoader`` to both guiders. Building it would put
      a node in front of ComfyUI whose only effect is to be there.
    * ``SimpleCalculatorKJ`` -> ``CM_IntBinaryOperation`` / ``CM_FloatBinaryOperation``, and
      only twice. The export computes six numbers in graph nodes; four of them —
      ``reference_seconds * frame_rate + 1``, that count over the frame rate, the same again
      over the *loaded* rate, and the extension's end time — are functions of arguments this
      builder already holds, because ``force_rate`` fixes the loaded rate to ``frame_rate``.
      They are folded into Python literals, the way ``build_h3_image_edit_payload`` resolves
      the export's Any Switches rather than reproducing them as plumbing, and
      ``tests/test_workflows.py`` re-derives all four from the export's own expressions and
      constants rather than trusting the arithmetic here. The two that survive genuinely
      depend on the file — the head's frame count and where the reference audio starts — and
      they move to ComfyMath's binary operations, from the same pack the export already draws
      ``CM_IntToFloat`` from, because ``SimpleCalculatorKJ.variables`` is a
      ``COMFY_AUTOGROW_V3`` group keyed by ``names`` with neither a ``prefix`` nor a ``max``,
      which the shared pre-flight cannot expand — its values would ride into ComfyUI unchecked.
    * ``ResizeImageMaskNode`` -> ``ImageScaleBy``, and ``ImpactImageInfo`` folded into the
      ``GetImageSizeAndCount`` already in the graph. The first is the same "scale by 0.5, area"
      the export performs, expressed on a core node instead of a ``COMFY_DYNAMICCOMBO_V3``
      whose ``resize_type.multiplier`` sub-input the shared pre-flight cannot expand either.
      The second is one measurement of one batch read twice: ``GetImageSizeAndCount`` publishes
      ``width``/``height``/``count`` of the same frames ``ImpactImageInfo`` was measuring.
    """
    if not isinstance(source_video, str) or not source_video.strip():
        raise ValueError("An LTX 2.5 extension needs a source video path")
    # VHS strips surrounding whitespace and one leading/trailing quote off the path before
    # opening it (``utils.strip_path``). The file a caller checked on disk and the file the
    # node then opens must be the same one, so a value that would be rewritten is refused
    # rather than silently repointed.
    if source_video != source_video.strip().strip('"'):
        raise ValueError(
            f"The source video path must not be quoted or padded: VHS would rewrite "
            f"{source_video!r} before opening it"
        )
    extension = source_video.rsplit(".", 1)[-1].lower() if "." in source_video else ""
    if extension not in LTX25_EXTEND_SOURCE_EXTENSIONS:
        raise ValueError(
            f"LTX 2.5 extension reads {', '.join(LTX25_EXTEND_SOURCE_EXTENSIONS)}; "
            f"{source_video} is not one of those"
        )
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError("An LTX 2.5 extension needs an output filename prefix")
    # ValueError on both, for the reason `_positive_integer` records.
    if not isinstance(prompt, str):
        raise ValueError(  # noqa: TRY004
            f"An LTX 2.5 extension's prompt must be text, not {prompt!r}"
        )
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(  # noqa: TRY004
            f"An LTX 2.5 extension's seed must be a whole number, not {seed!r}"
        )
    if not 0 <= seed <= LTX25_EXTEND_MAX_SEED:
        raise ValueError(
            f"RandomNoise.noise_seed takes 0 to {LTX25_EXTEND_MAX_SEED}; {seed} is outside it"
        )
    rate = _positive_integer("An LTX 2.5 extension's frame rate", frame_rate)
    if rate > LTX25_EXTEND_MAX_FRAME_RATE:
        raise ValueError(
            f"VHS_LoadVideoPath.force_rate tops out at {LTX25_EXTEND_MAX_FRAME_RATE} fps; "
            f"{rate} is above it"
        )
    for name, value in (("width", width), ("height", height)):
        _positive_integer(f"An LTX 2.5 extension's {name}", value)
        if value > LTX25_EXTEND_MAX_DIMENSION:
            raise ValueError(
                f"ImageResizeKJv2.{name} tops out at {LTX25_EXTEND_MAX_DIMENSION}; "
                f"{value} is above it"
            )
    reference = _positive_integer("An LTX 2.5 extension's reference window", reference_seconds)
    # The export's "(a*b)+1": one frame more than the seconds asked for, which is the extra
    # frame LTX's 8n+1 latent grid wants at the head of a conditioned window.
    reference_frames = reference * rate + 1
    if reference_frames > LTX25_EXTEND_MAX_RANGE_FRAMES:
        raise ValueError(
            f"GetImageRangeFromBatch.num_frames tops out at {LTX25_EXTEND_MAX_RANGE_FRAMES} "
            f"frames; a {reference}s reference at {rate} fps is {reference_frames}"
        )
    added = _finite("An LTX 2.5 extension's added length", extend_seconds)
    if added <= 0:
        raise ValueError(f"An extension must add more than zero seconds, not {added:g}s")
    # Where the reference window starts inside the padded latent, in seconds. The export
    # computes this twice — once against the framerate constant and once against the loaded
    # rate ``VHS_VideoInfo`` reports — and the two agree only because ``force_rate`` pins the
    # loaded rate to the constant. Folded to one number here for the same reason.
    reference_length = reference_frames / rate
    end_time = added + reference_length
    if end_time > LTX25_EXTEND_MAX_MASK_SECONDS:
        raise ValueError(
            f"LTXVAudioVideoMask spans at most {LTX25_EXTEND_MAX_MASK_SECONDS:g}s; a "
            f"{reference_length:g}s reference plus {added:g}s is {end_time:g}s"
        )
    payload: dict[str, dict[str, Any]] = {
        "mvp:source": {
            "class_type": "VHS_LoadVideoPath",
            "inputs": {
                "video": source_video,
                # A FLOAT input, and the export feeds it the framerate constant through a
                # ``CM_IntToFloat``. Forcing the rate is what makes every seconds-to-frames
                # number below computable without opening the file.
                "force_rate": float(rate),
                "custom_width": 0,
                "custom_height": 0,
                "frame_load_cap": 0,
                "skip_first_frames": 0,
                "select_every_nth": 1,
                # The export's. Unlike `AUDIO_REPLACE_SOURCE_FORMAT`, "LTXV" is kept here:
                # this *is* an LTX graph, its 8n+1 frame rule is the grid the sampler wants,
                # and conforming to it is why the output length is measured rather than
                # predicted.
                "format": "LTXV",
            },
        },
        "mvp:source_info": {
            "class_type": "VHS_VideoInfo",
            "inputs": {"video_info": ["mvp:source", 3]},
        },
        # Every frame at the working size. Output 1 is width, 2 is height, 3 is the count —
        # all three read off this one node, which is the `ImpactImageInfo` fold.
        "mvp:frames": {
            "class_type": "ImageResizeKJv2",
            "inputs": {
                "width": width,
                "height": height,
                "upscale_method": "nearest-exact",
                "keep_proportion": "stretch",
                "pad_color": "0, 0, 0",
                "crop_position": "center",
                "divisible_by": 64,
                "device": "cpu",
                "image": ["mvp:source", 0],
            },
        },
        "mvp:measure": {
            "class_type": "GetImageSizeAndCount",
            "inputs": {"image": ["mvp:frames", 0]},
        },
        # Two constants that could have been literals and deliberately are not.
        #
        # `preflight.unbounded_numeric_inputs` reports a numeric literal whose schema publishes
        # neither `min` nor `max`, because a value on such an input rides into ComfyUI with the
        # range check silently disabled. Both ComfyMath binary operations declare their operands
        # as bare `{"default": 0}`, so the reference window's length would have been exactly
        # that — twice. Routed through nodes that *do* declare a range, the numbers are checked
        # where they are stated, and the export's own `INTConstant` is what it used for the
        # first of them anyway. The two ceilings that matter — the range node's 4096 frames and
        # the mask's 10000 seconds — are refused above and checked against the live schema by
        # `tests/preflight_ltx25_extend.py`.
        "mvp:reference_frames": {
            "class_type": "INTConstant",
            "inputs": {"value": reference_frames},
        },
        "mvp:reference_length": {
            "class_type": "PrimitiveFloat",
            "inputs": {"value": reference_length},
        },
        # How many frames come back untouched: everything the reference window does not cover.
        # The one number here that genuinely cannot be computed without the file.
        "mvp:head_frames": {
            "class_type": "CM_IntBinaryOperation",
            "inputs": {"op": "Sub", "a": ["mvp:measure", 3], "b": ["mvp:reference_frames", 0]},
        },
        "mvp:head": {
            "class_type": "GetImageRangeFromBatch",
            "inputs": {
                "start_index": 0,
                "num_frames": ["mvp:head_frames", 0],
                "images": ["mvp:frames", 0],
            },
        },
        # `start_index: -1` is the node's "from the end" — the tail the model is conditioned on.
        "mvp:tail": {
            "class_type": "GetImageRangeFromBatch",
            "inputs": {
                "start_index": -1,
                "num_frames": ["mvp:reference_frames", 0],
                "images": ["mvp:frames", 0],
            },
        },
        "mvp:tail_half": {
            "class_type": "ImageScaleBy",
            "inputs": {
                "upscale_method": "area",
                "scale_by": LTX25_EXTEND_REFERENCE_SCALE,
                "image": ["mvp:tail", 0],
            },
        },
        "mvp:preprocess": {
            "class_type": "LTXVPreprocess",
            "inputs": {
                "img_compression": LTX25_EXTEND_IMG_COMPRESSION,
                "image": ["mvp:tail_half", 0],
            },
        },
        "mvp:encode_reference": {
            "class_type": "VAEEncodeTiled",
            "inputs": {
                "tile_size": 512,
                "overlap": 64,
                "temporal_size": 4096,
                "temporal_overlap": 8,
                "pixels": ["mvp:preprocess", 0],
                "vae": ["mvp:video_vae", 0],
            },
        },
        # The source's own track, normalised. Fed to the audio conditioning either way, and
        # to the saver only when `include_audio`.
        "mvp:loudness": {
            "class_type": "NormalizeAudioLoudness",
            "inputs": {"lufs": LTX25_EXTEND_LOUDNESS_LUFS, "audio": ["mvp:source", 2]},
        },
        # Output 7 of `VHS_VideoInfo` is `loaded_duration`. The second number that needs the
        # file: where the reference window's audio begins.
        "mvp:reference_audio_start": {
            "class_type": "CM_FloatBinaryOperation",
            "inputs": {"op": "Sub", "a": ["mvp:source_info", 7], "b": ["mvp:reference_length", 0]},
        },
        "mvp:reference_audio": {
            "class_type": "TrimAudioDuration",
            "inputs": {
                "start_index": ["mvp:reference_audio_start", 0],
                "duration": reference_length,
                "audio": ["mvp:loudness", 0],
            },
        },
        "mvp:encode_reference_audio": {
            "class_type": "LTXVAudioVAEEncode",
            "inputs": {"audio": ["mvp:reference_audio", 0], "audio_vae": ["mvp:audio_vae", 0]},
        },
        # The extension itself, expressed as a mask: keep the reference seconds, invent
        # everything from `reference_length` to `end_time`, and `pad` the latent out to hold
        # them rather than truncating to what the reference already covers.
        "mvp:mask": {
            "class_type": "LTXVAudioVideoMask",
            "inputs": {
                "video_fps": float(rate),
                "video_start_time": reference_length,
                "video_end_time": end_time,
                "audio_start_time": reference_length,
                "audio_end_time": end_time,
                "max_length": "pad",
                "existing_mask_mode": "add",
                "video_latent": ["mvp:encode_reference", 0],
                "audio_latent": ["mvp:encode_reference_audio", 0],
            },
        },
        "mvp:base_latent": {
            "class_type": "LTXVConcatAVLatent",
            "inputs": {"video_latent": ["mvp:mask", 0], "audio_latent": ["mvp:mask", 1]},
        },
        "mvp:model": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": LTX25_EXTEND_UNET, "weight_dtype": "default"},
        },
        "mvp:clip": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": LTX25_EXTEND_CLIP, "type": "ltxv", "device": "default"},
        },
        "mvp:video_vae": {
            "class_type": "VAELoaderKJ",
            "inputs": {
                "vae_name": LTX25_EXTEND_VIDEO_VAE,
                "device": "main_device",
                "weight_dtype": "bf16",
            },
        },
        # On the CPU, as the export loads it. The audio VAE is a dependency of this graph
        # whatever `include_audio` says: the conditioning goes through it.
        "mvp:audio_vae": {
            "class_type": "VAELoaderKJ",
            "inputs": {
                "vae_name": LTX25_EXTEND_AUDIO_VAE,
                "device": "cpu",
                "weight_dtype": "bf16",
            },
        },
        # Reached, unlike its namesake in the enhancer and audio-replacer exports.
        "mvp:upscaler": {
            "class_type": "LatentUpscaleModelLoader",
            "inputs": {"model_name": LTX25_EXTEND_UPSCALER},
        },
        "mvp:prompt": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["mvp:clip", 0]},
        },
        "mvp:negative": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": LTX25_EXTEND_NEGATIVE_PROMPT, "clip": ["mvp:clip", 0]},
        },
        "mvp:conditioning": {
            "class_type": "LTXVConditioning",
            "inputs": {
                "frame_rate": float(rate),
                "positive": ["mvp:prompt", 0],
                "negative": ["mvp:negative", 0],
            },
        },
        # One guider for both passes. The export builds two `CFGGuider` nodes with byte-identical
        # inputs, which is the editor's habit of not sharing a wire across a group rather than a
        # difference between the passes.
        "mvp:guider": {
            "class_type": "CFGGuider",
            "inputs": {
                "cfg": LTX25_EXTEND_CFG,
                "model": ["mvp:model", 0],
                "positive": ["mvp:conditioning", 0],
                "negative": ["mvp:conditioning", 1],
            },
        },
        "mvp:sampler": {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": LTX25_EXTEND_SAMPLER},
        },
        "mvp:base_sigmas": {
            "class_type": "ManualSigmas",
            "inputs": {"sigmas": LTX25_EXTEND_BASE_SIGMAS},
        },
        "mvp:base_noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        # Output 1 is `denoised_output`, which is what both passes of the export read.
        "mvp:base_sample": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["mvp:base_noise", 0],
                "guider": ["mvp:guider", 0],
                "sampler": ["mvp:sampler", 0],
                "sigmas": ["mvp:base_sigmas", 0],
                "latent_image": ["mvp:base_latent", 0],
            },
        },
        "mvp:base_split": {
            "class_type": "LTXVSeparateAVLatent",
            "inputs": {"av_latent": ["mvp:base_sample", 1]},
        },
        "mvp:upsample": {
            "class_type": "LTXVLatentUpsampler",
            "inputs": {
                "samples": ["mvp:base_split", 0],
                "upscale_model": ["mvp:upscaler", 0],
                "vae": ["mvp:video_vae", 0],
            },
        },
        # The first frame of the reference tail, put back into the upscaled latent at full
        # strength so the refine pass starts from the source's own picture rather than from
        # the low-res pass's idea of it.
        "mvp:anchor_frame": {
            "class_type": "GetImageRangeFromBatch",
            "inputs": {"start_index": 0, "num_frames": 1, "images": ["mvp:tail", 0]},
        },
        "mvp:anchor": {
            "class_type": "ResizeImagesByLongerEdge",
            "inputs": {
                "longer_edge": LTX25_EXTEND_ANCHOR_LONGER_EDGE,
                "images": ["mvp:anchor_frame", 0],
            },
        },
        "mvp:inplace": {
            "class_type": "LTXVImgToVideoInplace",
            "inputs": {
                "strength": 1.0,
                "bypass": False,
                "vae": ["mvp:video_vae", 0],
                "image": ["mvp:anchor", 0],
                "latent": ["mvp:upsample", 0],
            },
        },
        "mvp:refine_latent": {
            "class_type": "LTXVConcatAVLatent",
            "inputs": {
                "video_latent": ["mvp:inplace", 0],
                "audio_latent": ["mvp:base_split", 1],
            },
        },
        "mvp:refine_sigmas": {
            "class_type": "ManualSigmas",
            "inputs": {"sigmas": LTX25_EXTEND_REFINE_SIGMAS},
        },
        "mvp:refine_noise": {
            "class_type": "RandomNoise",
            "inputs": {"noise_seed": LTX25_EXTEND_REFINE_SEED},
        },
        "mvp:refine_sample": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["mvp:refine_noise", 0],
                "guider": ["mvp:guider", 0],
                "sampler": ["mvp:sampler", 0],
                "sigmas": ["mvp:refine_sigmas", 0],
                "latent_image": ["mvp:refine_latent", 0],
            },
        },
        "mvp:refine_split": {
            "class_type": "LTXVSeparateAVLatent",
            "inputs": {"av_latent": ["mvp:refine_sample", 1]},
        },
        "mvp:decode": {
            "class_type": "VAEDecodeTiled",
            "inputs": {
                "tile_size": 512,
                "overlap": 64,
                "temporal_size": 4096,
                "temporal_overlap": 8,
                "samples": ["mvp:refine_split", 0],
                "vae": ["mvp:video_vae", 0],
            },
        },
        # Back to the head's own frame size before the two batches meet, padded rather than
        # stretched: the upscaled latent does not decode to exactly the working size.
        "mvp:generated": {
            "class_type": "ImageResizeKJv2",
            "inputs": {
                "width": ["mvp:measure", 1],
                "height": ["mvp:measure", 2],
                "upscale_method": "nearest-exact",
                "keep_proportion": "pad_edge_pixel",
                "pad_color": "0, 0, 0",
                "crop_position": "center",
                "divisible_by": 64,
                "device": "cpu",
                "image": ["mvp:decode", 0],
            },
        },
        # The untouched head, then the model's rendering of the reference tail *and* the new
        # seconds. The tail appears once, not twice: `mvp:head` stops where it begins.
        "mvp:batch": {
            "class_type": "ImageBatchMulti",
            "inputs": {
                "inputcount": 2,
                "image_1": ["mvp:head", 0],
                "image_2": ["mvp:generated", 0],
            },
        },
        "mvp:output": {
            "class_type": "ImageResizeKJv2",
            "inputs": {
                "width": width,
                "height": height,
                "upscale_method": "lanczos",
                "keep_proportion": "stretch",
                "pad_color": "0, 0, 0",
                "crop_position": "center",
                "divisible_by": 8,
                "device": "cpu",
                "image": ["mvp:batch", 0],
            },
        },
        "mvp:save": {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "frame_rate": float(rate),
                "loop_count": 0,
                "filename_prefix": prefix,
                "format": "video/h264-mp4",
                "pix_fmt": "yuv420p",
                "crf": 19,
                "save_metadata": True,
                "trim_to_audio": False,
                "pingpong": False,
                "save_output": True,
                "images": ["mvp:output", 0],
            },
        },
    }
    if include_audio:
        payload["mvp:decode_audio"] = {
            "class_type": "LTXVAudioVAEDecode",
            "inputs": {
                "samples": ["mvp:refine_split", 1],
                "audio_vae": ["mvp:audio_vae", 0],
            },
        }
        # Only the invented seconds. The generated track also covers the reference window, and
        # that part is thrown away because the source's own audio for those seconds is already
        # in `audio1`.
        payload["mvp:generated_audio"] = {
            "class_type": "TrimAudioDuration",
            "inputs": {
                "start_index": reference_length,
                "duration": float(added),
                "audio": ["mvp:decode_audio", 0],
            },
        }
        payload["mvp:audio"] = {
            "class_type": "AudioConcat",
            "inputs": {
                "direction": "after",
                "audio1": ["mvp:loudness", 0],
                "audio2": ["mvp:generated_audio", 0],
            },
        }
        payload["mvp:save"]["inputs"]["audio"] = ["mvp:audio", 0]
    return payload
