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
    request_timeout: float = Field(default=30.0, gt=0)
    max_upload_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)

    @property
    def workflow_root(self) -> Path:
        return self.comfy_root / "user" / "default" / "workflows"
