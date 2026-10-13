"""Authentication for the M365 Copilot (Sydney) backend.

There is no API key: the gateway impersonates Microsoft's own Office-web
Copilot client through an OAuth2 PKCE flow, then keeps a refresh token on disk
so later runs stay silent.

Flow:
  1. ``ensure_token()`` first tries a cached access token, then a refresh-token
     grant (plain HTTPS, no browser).
  2. If neither works, it opens a visible browser window at the Microsoft
     sign-in page. The ``?code=`` is scraped from the *navigation request* to
     the ``nativeclient`` redirect URI, because a real browser follows it one
     hop further (to ``/common/wrongplace``) and the code would otherwise be
     missed.
  3. The code is exchanged for tokens; the rotated refresh token is persisted.

Tokens live in ``storage/tokens.json`` (gitignored, mode 0600).
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx

from copilot_gateway.config import Settings
from copilot_gateway.provider.errors import AuthRequired, ProviderUnavailable
from copilot_gateway.utils.logging import get_logger

log = get_logger("m365auth")

AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
# Microsoft's first-party Office-web Copilot client. We did not register this.
DEFAULT_CLIENT_ID = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"
REDIRECT_URI = "https://login.microsoftonline.com/common/oauth2/nativeclient"
SCOPES = [
    "https://substrate.office.com/sydney/M365Chat.Read",
    "https://substrate.office.com/sydney/sydney.readwrite",
]

LOGIN_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    # Epoch seconds when the access token expires.
    expires_at: float


def generate_pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge) per RFC 7636."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(client_id: str, verifier: str, scopes: list[str] | None = None) -> tuple[str, str]:
    """Build the sign-in URL. Returns (url, code_challenge)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(scopes or SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}", challenge


def decode_jwt_payload(token: str) -> dict:
    """Decode the JWT payload without verifying the signature.

    This is our own token, already validated by Microsoft; we only need the
    ``oid``/``tid`` claims to build the ChatHub URL.
    """
    try:
        payload = token.split(".")[1]
    except IndexError as exc:
        raise AuthRequired("The stored access token is not a JWT") from exc
    padded = payload + "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AuthRequired("The stored access token is not a valid JWT") from exc


class TokenStore:
    """Persists the refresh token (and a short-lived access token) as JSON."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> TokenSet | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return TokenSet(
                access_token=raw["access_token"],
                refresh_token=raw.get("refresh_token"),
                expires_at=float(raw.get("expires_at", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def save(self, tokens: TokenSet) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "access_token": tokens.access_token,
                    "refresh_token": tokens.refresh_token,
                    "expires_at": tokens.expires_at,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)


async def _post_token_form(data: dict[str, str]) -> TokenSet:
    # trust_env=False: some environments set proxy variables that httpx cannot
    # parse; the login/token endpoints are reached directly.
    async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
        response = await http.post(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or "access_token" not in body:
        detail = body.get("error_description") or body.get("error") or f"HTTP {response.status_code}"
        raise AuthRequired(f"Microsoft token endpoint refused the grant: {detail}")
    skew = 60
    return TokenSet(
        access_token=body["access_token"],
        # AAD rotates the refresh token on every grant; persist the newest one.
        refresh_token=body.get("refresh_token"),
        expires_at=time.time() + float(body.get("expires_in", 3600)) - skew,
    )


async def exchange_code(code: str, verifier: str, client_id: str) -> TokenSet:
    return await _post_token_form(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        }
    )


async def refresh_grant(refresh_token: str, client_id: str, scopes: list[str] | None = None) -> TokenSet:
    return await _post_token_form(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": " ".join(scopes or SCOPES),
        }
    )


async def interactive_login(settings: Settings) -> TokenSet:
    """Open a visible browser for the one-time Microsoft sign-in.

    The auth code is captured from the navigation *request* to the nativeclient
    redirect URI, because the page itself bounces to /common/wrongplace.
    """
    from playwright.async_api import async_playwright

    verifier, _ = generate_pkce_pair()
    auth_url, _ = build_authorize_url(settings.m365_client_id, verifier)
    log.info("Opening a browser window for Microsoft sign-in")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(settings.m365_profile_dir),
            headless=False,
            executable_path=settings.chromium_path,
            user_agent=LOGIN_USER_AGENT,
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = browser.pages[0] if browser.pages else await browser.new_page()
            loop = asyncio.get_running_loop()
            code_future: asyncio.Future[str] = loop.create_future()

            def on_request(request) -> None:  # noqa: ANN001
                url = request.url
                if "/oauth2/nativeclient" in url and not code_future.done():
                    query = dict(parse_qsl(urlparse(url).query))
                    code = query.get("code")
                    if code:
                        log.info("Captured auth code from the nativeclient redirect")
                        code_future.set_result(code)

            page.on("request", on_request)
            await page.goto(auth_url, wait_until="domcontentloaded")
            try:
                code = await asyncio.wait_for(code_future, timeout=settings.m365_auth_timeout)
            except TimeoutError as exc:
                raise AuthRequired(
                    "Timed out waiting for Microsoft sign-in. "
                    "Run again and complete the sign-in in the browser window."
                ) from exc
        finally:
            await browser.close()

    tokens = await exchange_code(code, verifier, settings.m365_client_id)
    TokenStore(settings.token_path).save(tokens)
    log.info("Microsoft sign-in succeeded; tokens saved")
    return tokens


class TokenManager:
    """Hands out valid access tokens, refreshing or logging in as needed."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._store = TokenStore(settings.token_path)
        self._lock = asyncio.Lock()
        self._login_task: asyncio.Task[TokenSet] | None = None

    def start_interactive_login(self) -> asyncio.Task[TokenSet]:
        """Kick off the visible-browser sign-in in the background (idempotent)."""
        if self._login_task is None or self._login_task.done():
            self._login_task = asyncio.create_task(interactive_login(self._settings))
        return self._login_task

    async def get_token(self, *, interactive: bool = True) -> str:
        """Return a valid access token.

        With interactive=True (default), a missing/expired credential opens the
        sign-in browser window and waits for the user.
        """
        async with self._lock:
            # 1. An explicitly supplied access token (short-lived, ~1h).
            if self._settings.m365_access_token:
                return self._settings.m365_access_token

            # 2. A cached access token that is still valid.
            saved = self._store.load()
            if saved and saved.expires_at > time.time():
                return saved.access_token

            # 3. Refresh-token grant: env or previously saved. No browser needed.
            refresh_token = self._settings.m365_refresh_token or (saved.refresh_token if saved else None)
            if refresh_token:
                try:
                    tokens = await refresh_grant(refresh_token, self._settings.m365_client_id)
                except AuthRequired:
                    log.warning("Stored refresh token was rejected; falling back to sign-in")
                else:
                    if not tokens.refresh_token:
                        tokens.refresh_token = refresh_token
                    self._store.save(tokens)
                    return tokens.access_token

            # 4. Visible browser sign-in.
            if not interactive:
                raise AuthRequired(
                    "No valid Microsoft token. Set M365_REFRESH_TOKEN, or run with "
                    "interactive sign-in enabled to open the browser login."
                )
            log.info("No usable token; opening the Microsoft sign-in window")
            task = self.start_interactive_login()
            try:
                tokens = await asyncio.shield(task)
            except AuthRequired:
                raise
            except Exception as exc:
                raise ProviderUnavailable(f"Microsoft sign-in failed: {exc}") from exc
            return tokens.access_token
