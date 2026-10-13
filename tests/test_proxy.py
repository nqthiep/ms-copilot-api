"""Checks that proxy credentials let Chromium through an authenticating proxy without a popup."""

import asyncio
import base64
import functools
import http.client
import http.server
import threading
import urllib.parse
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError

from copilot_gateway.browser.session import BrowserSession

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_AUTH = "Basic " + base64.b64encode(b"corp-user:secret").decode()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def upstream_port():
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=str(FIXTURES))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture(scope="module")
def auth_proxy(upstream_port):
    """Forward proxy that answers 407 unless the request carries the expected credentials."""
    seen: list[str | None] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Proxy-Authorization"))
            if self.headers.get("Proxy-Authorization") != EXPECTED_AUTH:
                self.send_response(407)
                self.send_header("Proxy-Authenticate", 'Basic realm="corp"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            target = urllib.parse.urlsplit(self.path)
            path = target.path + (f"?{target.query}" if target.query else "")
            upstream = http.client.HTTPConnection("127.0.0.1", upstream_port)
            upstream.request("GET", path, headers={"Host": target.netloc})
            response = upstream.getresponse()
            body = response.read()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "text/html"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], seen
    server.shutdown()


def open_through_proxy(settings):
    async def scenario():
        browser = BrowserSession(settings)
        await browser.start()
        try:
            page = await browser.page()
            response = await page.goto(settings.copilot_url)
            return response.status, await page.title()
        finally:
            await browser.close()

    return asyncio.run(scenario())


def test_stored_credentials_pass_the_proxy_without_a_prompt(settings_factory, auth_proxy):
    proxy_port, seen = auth_proxy
    settings = settings_factory(
        "http://mock.test/mock_copilot.html",
        proxy_server=f"http://127.0.0.1:{proxy_port}",
        proxy_username="corp-user",
        proxy_password="secret",
    )

    status, title = open_through_proxy(settings)

    assert status == 200
    assert title == "Mock Copilot"
    assert EXPECTED_AUTH in seen


def test_proxy_challenge_fails_without_credentials(settings_factory, auth_proxy):
    # Without credentials the proxy's 407 challenge cannot be answered. A headed browser would
    # show the login popup; headless Chromium fails the navigation (headless-shell instead
    # surfaces the 407 response).
    proxy_port, _ = auth_proxy
    settings = settings_factory(
        "http://mock.test/mock_copilot.html",
        proxy_server=f"http://127.0.0.1:{proxy_port}",
    )

    try:
        status, _ = open_through_proxy(settings)
    except PlaywrightError as exc:
        assert "ERR_INVALID_AUTH_CREDENTIALS" in str(exc)
    else:
        assert status == 407


def test_password_is_not_shown_in_settings_repr(settings_factory):
    settings = settings_factory(proxy_password="do-not-log-me")

    assert "do-not-log-me" not in repr(settings)
