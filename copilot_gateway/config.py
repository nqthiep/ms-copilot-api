import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
PACKAGE_DIR = Path(__file__).resolve().parent
ENV_FILE = PACKAGE_DIR.parent / ".env"


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    copilot_url: str
    profile_dir: Path
    sessions_path: Path
    debug_dir: Path
    headless: bool
    chromium_path: str | None
    request_timeout: float
    max_retry: int
    login_timeout: float
    stable_seconds: float
    max_inline_chars: int
    max_file_bytes: int
    hide_when_ready: bool = False
    proxy_server: str | None = None
    proxy_username: str | None = None
    # Kept out of repr so the password never shows up in logs or tracebacks.
    proxy_password: str | None = field(default=None, repr=False)


def load_settings(env_file: Path = ENV_FILE) -> Settings:
    # Variables already set in the environment win over values in the file.
    load_dotenv(env_file)
    host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"GATEWAY_HOST must be a loopback address, got {host!r}")

    return Settings(
        host=host,
        port=int(os.getenv("GATEWAY_PORT", "8000")),
        copilot_url=os.getenv("COPILOT_URL", "https://copilot.cloud.microsoft/"),
        profile_dir=Path(
            os.getenv("PLAYWRIGHT_PROFILE_DIR", PACKAGE_DIR.parent / "playwright-profile")
        ).resolve(),
        sessions_path=Path(
            os.getenv("SESSIONS_PATH", PACKAGE_DIR / "storage" / "sessions.json")
        ).resolve(),
        debug_dir=Path(os.getenv("DEBUG_DIR", PACKAGE_DIR / "storage" / "debug")).resolve(),
        headless=os.getenv("HEADLESS", "false").lower() == "true",
        hide_when_ready=os.getenv("HIDE_WHEN_READY", "false").lower() == "true",
        proxy_server=os.getenv("PROXY_SERVER") or None,
        proxy_username=os.getenv("PROXY_USERNAME") or None,
        proxy_password=os.getenv("PROXY_PASSWORD") or None,
        chromium_path=os.getenv("CHROMIUM_PATH") or None,
        request_timeout=float(os.getenv("REQUEST_TIMEOUT", "300")),
        max_retry=int(os.getenv("MAX_RETRY", "3")),
        login_timeout=float(os.getenv("LOGIN_TIMEOUT", "600")),
        stable_seconds=float(os.getenv("RESPONSE_STABLE_SECONDS", "3")),
        max_inline_chars=int(os.getenv("MAX_INLINE_CHARS", "2000")),
        max_file_bytes=int(os.getenv("MAX_FILE_BYTES", str(20 * 1024 * 1024))),
    )
