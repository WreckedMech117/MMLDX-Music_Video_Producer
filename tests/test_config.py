from pathlib import Path

from music_video_producer.config import Settings


def test_settings_have_standalone_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MVP_COMFY_URL", raising=False)
    settings = Settings(data_root=tmp_path)

    assert settings.comfy_url == "http://127.0.0.1:8188"
    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 8765
    assert settings.data_root == tmp_path
    assert "Agent-OS" not in str(settings.data_root)


def test_settings_allow_environment_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MVP_COMFY_URL", "http://127.0.0.1:9000")
    settings = Settings(data_root=tmp_path)

    assert settings.comfy_url == "http://127.0.0.1:9000"
