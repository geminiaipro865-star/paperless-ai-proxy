"""Configuration defaults that are part of the public deployment contract."""

from chatgpt_proxy.config import Settings


def test_default_model_is_current_luna(monkeypatch):
    monkeypatch.delenv("CHATGPT_MODEL", raising=False)

    settings = Settings.from_env()

    assert settings.default_model == "gpt-5.6-luna"
