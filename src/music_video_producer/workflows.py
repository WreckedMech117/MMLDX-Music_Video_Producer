from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


def build_h3_director_payload(
    *,
    timeline_data: str,
    duration: float,
    requested_frames: int,  # must already sit on H3's 17k+5 grid; see timeline.align_h3_frames
    seed: int,
    width: int,
    height: int,
    steps: int,
    prefix: str,
    start: float = 0,
) -> dict[str, dict[str, Any]]:
    """Build the selected text-only H3 Director path with explicit loaders."""
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
                "start_frame": round(start * 24), "end_frame": round(end * 24), "duration_frames": requested_frames,
                "timeline_data": json.dumps(timeline, separators=(",", ":")),
                "local_prompts": local_prompts, "segment_lengths": segment_lengths, "guide_strength": "",
                "use_custom_audio": False, "use_custom_motion": False, "inpaint_audio": True,
                "frame_rate": 24, "display_mode": "seconds", "custom_width": width, "custom_height": height,
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


def build_h3_reference_payload(
    *,
    prompt: str,
    references: list[dict[str, Any]],
    duration: float,
    width: int,
    height: int,
    steps: int,
    seed: int,
    prefix: str,
    ref_image_size: str = "match",
) -> dict[str, dict[str, Any]]:
    """Build the audited H3 Ultra references-to-video path."""
    counts = {
        kind: sum(1 for item in references if item.get("kind") == kind)
        for kind in ("picture", "video", "audio")
    }
    if counts["picture"] > 9 or counts["video"] > 3 or counts["audio"] > 3:
        raise ValueError("H3 accepts at most 9 pictures, 3 videos, and 3 standalone audios")
    if not references:
        raise ValueError("At least one H3 reference is required")
    if ref_image_size not in {"match", "max"}:
        raise ValueError("ref_image_size must be 'match' or 'max'")
    requested = max(5, round(duration * 24))
    length = requested + (5 - requested % 17) % 17
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
    picture_index = video_index = audio_index = 0
    for item in references:
        kind = item.get("kind")
        if kind == "picture":
            condition_inputs[f"ref_images.ref_image_{picture_index}"] = ["mvp:split", picture_index]
            picture_index += 1
        elif kind == "video":
            condition_inputs[f"ref_videos.ref_video_{video_index}"] = ["mvp:split", 9 + video_index]
            if item.get("has_audio") and item.get("audio_mode", "paired") == "paired":
                condition_inputs[f"ref_video_audios.ref_video_audio_{video_index}"] = [
                    "mvp:split", 12 + video_index,
                ]
            video_index += 1
        elif kind == "audio":
            condition_inputs[f"ref_audios.ref_audio_{audio_index}"] = ["mvp:split", 15 + audio_index]
            audio_index += 1
        else:
            raise ValueError(f"Unsupported H3 reference kind: {kind}")
    return {
        "mvp:model": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "mvp:shift": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["mvp:model", 0], "shift_video": 12, "shift_audio": 3}},
        "mvp:attention": {"class_type": "PathchSageAttentionKJ", "inputs": {"model": ["mvp:shift", 0], "sage_attention": "disabled", "allow_compile": False}},
        "mvp:preview": {"class_type": "ModelPreviewOverrideKJ", "inputs": {"model": ["mvp:attention", 0], "max_resolution": 1024, "jpeg_quality": 80, "suppress_default_preview": True, "preview_frames": 12, "preview_fps": 12}},
        "mvp:clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "type": "minimax", "device": "default"}},
        "mvp:video_vae": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "mvp:audio_vae": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "mvp:references": {"class_type": "MiniMaxH3MediaLoader", "inputs": {"media_state": media_state}},
        "mvp:split": {"class_type": "MiniMaxH3ReferenceSplitter", "inputs": {"references": ["mvp:references", 0]}},
        "mvp:condition": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": condition_inputs},
        "mvp:sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "mvp:scheduler": {"class_type": "BasicScheduler", "inputs": {"model": ["mvp:preview", 0], "scheduler": "simple", "steps": steps, "denoise": 1.0}},
        "mvp:noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "mvp:guider": {"class_type": "BasicGuider", "inputs": {"model": ["mvp:preview", 0], "conditioning": ["mvp:condition", 0]}},
        "mvp:sample": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["mvp:noise", 0], "guider": ["mvp:guider", 0], "sampler": ["mvp:sampler", 0], "sigmas": ["mvp:scheduler", 0], "latent_image": ["mvp:condition", 1]}},
        "mvp:video": {"class_type": "VAEDecode", "inputs": {"samples": ["mvp:sample", 0], "vae": ["mvp:video_vae", 0]}},
        "mvp:audio": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["mvp:sample", 0], "vae": ["mvp:audio_vae", 0]}},
        "mvp:save": {"class_type": "VHS_VideoCombine", "inputs": {"images": ["mvp:video", 0], "audio": ["mvp:audio", 0], "frame_rate": 24, "loop_count": 0, "filename_prefix": prefix, "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 19, "save_metadata": True, "trim_to_audio": False, "pingpong": False, "save_output": True}},
    }


def patch_ltx25_dimension_boundary(
    template: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Patch the audited combined LTX 2.5 export without mutating its source.

    This is reference adaptation only; the full combined graph is not exposed
    for submission because it still contains creator-specific media paths and
    multiple expensive output branches.
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
    payload["mvp:ltx-size-normalize"] = {
        "class_type": "ImageResizeKJv2",
        "inputs": {
            "image": ["6112", 0],
            "width": 0,
            "height": 0,
            "upscale_method": "lanczos",
            "keep_proportion": "resize",
            "pad_color": "0, 0, 0",
            "crop_position": "center",
            "divisible_by": 16,
            "device": "cpu",
        },
    }
    normalized = ["mvp:ltx-size-normalize", 0]
    payload["6116:6070"]["inputs"]["pixels"] = normalized
    payload["6116:4970"]["inputs"]["image"] = normalized
    payload["6116:6073"]["inputs"]["image"] = normalized
    return payload


def build_multiview_payload(
    *, image_name: str, prompt: str, seed: int, prefix: str
) -> dict[str, dict[str, Any]]:
    """Krea 2 identity edit using the installed QuadView character-sheet LoRA."""
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
                "steps": 10,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "positive": ["119", 0],
                "negative": ["85", 0],
                "latent_image": ["135", 0],
                "denoise": 1.0,
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
