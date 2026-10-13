import os
from pathlib import Path

import pytest

from copilot_gateway.config import Settings

CHROMIUM_PATH = os.getenv("CHROMIUM_PATH") or None


def make_settings(tmp_path: Path, copilot_url: str = "http://127.0.0.1:1/", **overrides) -> Settings:
    values = dict(
        host="127.0.0.1",
        port=8000,
        copilot_url=copilot_url,
        profile_dir=tmp_path / "profile",
        sessions_path=tmp_path / "sessions.json",
        debug_dir=tmp_path / "debug",
        headless=True,
        chromium_path=CHROMIUM_PATH,
        request_timeout=60,
        max_retry=1,
        login_timeout=5,
        stable_seconds=0.5,
        max_inline_chars=30_000,
        max_file_bytes=1_000_000,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings_factory(tmp_path):
    def factory(copilot_url: str = "http://127.0.0.1:1/", **overrides) -> Settings:
        return make_settings(tmp_path, copilot_url, **overrides)

    return factory
