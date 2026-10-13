import pytest

from copilot_gateway.config import load_settings


def test_default_binding_is_loopback(monkeypatch, tmp_path):
    monkeypatch.delenv("GATEWAY_HOST", raising=False)

    assert load_settings(tmp_path / "none.env").host == "127.0.0.1"


def test_non_loopback_host_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_HOST", "0.0.0.0")

    with pytest.raises(ValueError, match="loopback"):
        load_settings(tmp_path / "none.env")


def test_values_are_read_from_the_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GATEWAY_PORT=9001\nHEADLESS=true\n", encoding="utf-8")
    # setenv then delenv: monkeypatch restores the original state afterwards, so values
    # loaded from the file do not leak into other tests.
    monkeypatch.setenv("GATEWAY_PORT", "")
    monkeypatch.delenv("GATEWAY_PORT")
    monkeypatch.setenv("HEADLESS", "")
    monkeypatch.delenv("HEADLESS")

    settings = load_settings(env_file)

    assert settings.port == 9001
    assert settings.headless is True


def test_real_environment_overrides_the_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GATEWAY_PORT=9001\n", encoding="utf-8")
    monkeypatch.setenv("GATEWAY_PORT", "9002")

    assert load_settings(env_file).port == 9002
