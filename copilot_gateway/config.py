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
    # Chat backend: "sydney" talks to the M365 Copilot SignalR API directly,
    # "browser" drives the Copilot web UI with Playwright (fallback).
    provider: str = "sydney"
    # Microsoft first-party Office-web Copilot client id (used for PKCE login).
    m365_client_id: str = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"
    # Kept out of repr so tokens never show up in logs or tracebacks.
    m365_refresh_token: str | None = field(default=None, repr=False)
    m365_access_token: str | None = field(default=None, repr=False)
    m365_auth_timeout: float = 600
    m365_default_tone: str = "magic"
    m365_no_code_interpreter: bool = False
    token_path: Path = field(default_factory=lambda: PACKAGE_DIR / "storage" / "tokens.json")
    m365_profile_dir: Path = field(
        default_factory=lambda: (PACKAGE_DIR.parent / "m365-profile").resolve()
    )
    # Text files larger than this are rejected instead of inlined into the prompt.
    max_inline_file_chars: int = 100_000


def load_settings(env_file: Path = ENV_FILE) -> Settings:
    # Variables already set in the environment win over values in the file.
    load_dotenv(env_file)
    host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"GATEWAY_HOST must be a loopback address, got {host!r}")
    provider = os.getenv("COPILOT_PROVIDER", "sydney").lower()
    if provider not in ("sydney", "browser"):
        raise ValueError(f"COPILOT_PROVIDER must be 'sydney' or 'browser', got {provider!r}")

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
        provider=provider,
        m365_client_id=os.getenv("M365_CLIENT_ID", "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"),
        m365_refresh_token=os.getenv("M365_REFRESH_TOKEN") or None,
        m365_access_token=os.getenv("M365_ACCESS_TOKEN") or None,
        m365_auth_timeout=float(os.getenv("M365_AUTH_TIMEOUT", "600")),
        m365_default_tone=os.getenv("M365_DEFAULT_TONE", "magic"),
        m365_no_code_interpreter=os.getenv("M365_NO_CODE_INTERPRETER", "false").lower() == "true",
        token_path=Path(
            os.getenv("TOKEN_PATH", PACKAGE_DIR / "storage" / "tokens.json")
        ).resolve(),
        m365_profile_dir=Path(
            os.getenv("M365_PROFILE_DIR", PACKAGE_DIR.parent / "m365-profile")
        ).resolve(),
        max_inline_file_chars=int(os.getenv("MAX_INLINE_FILE_CHARS", "100000")),
    )
