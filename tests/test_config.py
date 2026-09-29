"""Tests for upwork_scout.config: loading config.yaml, secrets and profile-dir safety."""

from __future__ import annotations

from pathlib import Path

import pytest

import pydantic

from upwork_scout.config import (
    ConfigError,
    TranslateCfg,
    load_config,
    load_secrets,
    validate_profile_dir,
)


class TestLoadConfig:
    def test_real_config_loads(self):
        cfg = load_config()
        assert cfg.browser.channel == "chrome"
        assert cfg.upwork.find_work_url.startswith("https://www.upwork.com/nx/find-work")

    def test_collection_source_defaults_to_browser(self):
        from upwork_scout.config import CollectionCfg

        assert CollectionCfg().source == "browser"

    def test_translate_section_loads_from_config_yaml(self):
        cfg = load_config()
        assert cfg.translate.enabled is True
        assert cfg.translate.model == "google/gemini-2.5-flash-lite"
        assert cfg.translate.api_url == "https://openrouter.ai/api/v1/chat/completions"
        assert cfg.translate.scope == "passed"

    def test_inbox_section_loads_from_config_yaml(self):
        cfg = load_config()
        assert cfg.inbox.folder == "inbox"
        assert cfg.inbox.move_processed is True


class TestTranslateApiUrlValidation:
    def test_malformed_port_rejected(self):
        with pytest.raises(pydantic.ValidationError):
            TranslateCfg(api_url="https://example.com:abc/x")

    def test_missing_host_rejected(self):
        with pytest.raises(pydantic.ValidationError):
            TranslateCfg(api_url="https:///x")

    def test_via_load_config(self, tmp_path: Path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("translate:\n  api_url: https://example.com:abc/x\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(bad)

    def test_invalid_yaml_raises_config_error(self, tmp_path: Path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("key: [unclosed", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(bad)

    def test_invalid_value_raises_config_error(self, tmp_path: Path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("scoring:\n  min_score: 500\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(bad)

    def test_missing_file_raises_config_error(self, tmp_path: Path):
        with pytest.raises(ConfigError):
            load_config(tmp_path / "does-not-exist.yaml")


class TestValidateProfileDir:
    def test_rejects_daily_chrome_profile(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        daily = tmp_path / "Google" / "Chrome" / "User Data"
        with pytest.raises(ConfigError):
            validate_profile_dir(daily)

    def test_rejects_subfolder_of_daily_profile(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        sub = tmp_path / "Google" / "Chrome" / "User Data" / "Default"
        with pytest.raises(ConfigError):
            validate_profile_dir(sub)

    def test_dedicated_profile_dir_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        validate_profile_dir(tmp_path / "browser-profile")  # must not raise


class TestLoadSecrets:
    def test_nonempty_env_file_value_wins_over_process_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("OPENROUTER_API_KEY=filekey\n", encoding="utf-8")
        monkeypatch.setenv("OPENROUTER_API_KEY", "envkey")
        secrets = load_secrets(tmp_path)
        assert secrets.openrouter_api_key == "filekey"

    def test_empty_env_file_value_falls_back_to_process_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("JEV_MODEL=\n", encoding="utf-8")
        monkeypatch.setenv("JEV_MODEL", "envmodel")
        secrets = load_secrets(tmp_path)
        assert secrets.jev_model == "envmodel"

    def test_non_https_jev_api_url_raises(self, tmp_path: Path):
        (tmp_path / ".env").write_text("JEV_API_URL=http://insecure.example.com\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_secrets(tmp_path)

    def test_secrets_repr_never_contains_key(self, tmp_path: Path):
        (tmp_path / ".env").write_text("OPENROUTER_API_KEY=super-secret-value\n", encoding="utf-8")
        secrets = load_secrets(tmp_path)
        assert "super-secret-value" not in repr(secrets)
