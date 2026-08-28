from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMFY_ROOT = Path(
    r"J:\Hermes-Remote\comfyui\ComfyUI_windows_portable\ComfyUI"
)


class Settings(BaseSettings):
    """Runtime settings with an MVP_ environment prefix."""

    model_config = SettingsConfigDict(
        env_prefix="MVP_",
        env_file=PROJECT_ROOT / ".env",
        extra="ignore",
    )

    app_host: str = "127.0.0.1"
    app_port: int = 8765
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_root: Path = DEFAULT_COMFY_ROOT
    #: Where projects, the LUT folder and machine preferences live. **Anchored to an absolute
    #: path by `_anchor_data_root` below**, and that is not cosmetic — see the validator.
    data_root: Path = PROJECT_ROOT / "data"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    #: On by default. The language model and ComfyUI share one card, and a model left
    #: resident is the difference between a render that allocates and one that spills to
    #: system RAM. Defaulting it on is safe because every way it can go wrong ends with the
    #: render submitted anyway: with no host configured it never fires, with nothing loaded
    #: it costs one local HTTP call, and with no `lms` CLI present it records a line and
    #: gets out of the way. Set MVP_LLM_EJECT_BEFORE_RENDER=false to turn it off.
    llm_eject_before_render: bool = True
    #: Path to LM Studio's `lms` executable. Blank means "find it" — PATH, then
    #: ~/.lmstudio/bin. The arguments are not configurable; see `vram.CliUnloader`.
    llm_eject_executable: str = ""
    #: Ceiling on how long a render may wait for the eject before giving up on it.
    llm_eject_timeout: float = Field(default=20.0, gt=0)
    request_timeout: float = Field(default=30.0, gt=0)
    max_upload_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    #: SageAttention mode for every H3 submission carrying a `PathchSageAttentionKJ` node.
    #: Blank means "leave the adapters' evidence value alone" (the exports carry
    #: `disabled` because their creator launches ComfyUI with `--use-sage-attention`);
    #: any other value is patched onto the node at submission by `create_app`'s one
    #: choke point. "auto" is the sensible opt-in; the exact kernels are the node's own
    #: options. Requires the `sageattention` package inside ComfyUI's python — installed
    #: 2026-08-19 (woct0rdho 2.2.0+cu128torch2.7.1, kernel-probed on the RTX 5090).
    sage_attention: str = ""

    @field_validator("data_root")
    @classmethod
    def _anchor_data_root(cls, value: Path) -> Path:
        """A relative `MVP_DATA_ROOT` is anchored to this process's directory before anything
        reads it.

        **Because one path derived from this root reaches ffmpeg's argv, and ffmpeg does not
        always run in this process's directory.** A bound Shot's render sets `cwd` to the
        export's `workdir` or to `previews/` so a `sendcmd` script can be a bare relative
        filename (R-30); `lut3d=file=` is the only other filesystem reference in the composed
        chain, and it is built from `discover_luts(settings.data_root)`. Under
        `MVP_DATA_ROOT=data` the entry's path stayed `data/luts/warm-shift.cube`, so a Shot
        carrying **both** a binding and a Grade card composed a chain ffmpeg read from inside
        `.work-<job>/` and could not find. Measured 2026-08-28 through real ffmpeg: rc -2,
        `Parsed_lut3d: data/luts/warm-shift.cube: No such file or directory`, and the last line
        of the stderr the Director is shown blames the *output* file rather than the grade.

        The export's `workdir` and the preview's `previews_root` were each given a `.resolve()`
        when the `cwd` arrived; this is the third path that needed one, and it is fixed at the
        root rather than at the call site so `ProjectStore` and `MachinePreferences` are anchored
        by the same rule.

        **An already-absolute root is returned untouched, deliberately.** `resolve()` also
        follows junctions and symlinks, and this value ends up inside the composed chain, which
        is the fourth input to `effects.preview_fingerprint` — rewriting an absolute root that
        happens to sit behind a junction would rename every cached preview on that machine for a
        look that did not change. Relativity is the whole fault, so relativity is the whole fix.
        """
        return value if value.is_absolute() else value.resolve()

    @property
    def workflow_root(self) -> Path:
        return self.comfy_root / "user" / "default" / "workflows"
