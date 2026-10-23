"""Guard: the test session never reads a .env file and can never reach Firebase."""

from pathlib import Path

import pytest

from app.core import config
from app.core.config import Settings, settings
from app.integrations.firebase import push


def test_settings_were_not_loaded_from_a_dotenv_file() -> None:
    assert config.ENV_FILE is None
    assert Settings.model_config.get("env_file") is None
    assert settings.model_config.get("env_file") is None


def test_a_dotenv_file_in_the_working_directory_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("APP_NAME=read-from-dotenv\nMAX_UPLOAD_MB=99\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    fresh = Settings()
    assert fresh.APP_NAME == "Lamsa API" and fresh.MAX_UPLOAD_MB == 5


def test_push_backend_is_forced_to_log() -> None:
    assert (settings.APP_ENV, settings.PUSH_BACKEND, settings.FIREBASE_CREDENTIALS_PATH) == ("test", "log", None)
    assert settings.run_worker_in_app is False
    push._configured_sender.cache_clear()
    try:
        assert isinstance(push._configured_sender(), push.LogPushSender)  # even without the fake sender
    finally:
        push._configured_sender.cache_clear()
    assert not isinstance(push.get_push_sender(), push.FirebasePushSender)
