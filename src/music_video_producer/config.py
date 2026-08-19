from pathlib import Path

from pydantic import Field
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

    @property
    def workflow_root(self) -> Path:
        return self.comfy_root / "user" / "default" / "workflows"
