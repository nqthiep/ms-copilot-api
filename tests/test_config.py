import pytest

from copilot_gateway.config import load_settings


def test_default_binding_is_loopback(monkeypatch):
    monkeypatch.delenv("GATEWAY_HOST", raising=False)

    assert load_settings().host == "127.0.0.1"


def test_non_loopback_host_is_refused(monkeypatch):
    monkeypatch.setenv("GATEWAY_HOST", "0.0.0.0")

    with pytest.raises(ValueError, match="loopback"):
        load_settings()
