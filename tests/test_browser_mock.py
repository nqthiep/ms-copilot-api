"""Drives a real Chromium against a mock Copilot page served on localhost.

The mock uses the same selectors as copilot_gateway/browser/selectors.py. These tests
check the gateway's browser logic. They do not prove the real Copilot page matches.
"""

import asyncio
import functools
import http.server
import threading
from pathlib import Path

import pytest

from copilot_gateway.browser.session import BrowserSession
from copilot_gateway.provider.copilot_provider import CopilotProvider
from copilot_gateway.provider.errors import ProviderUnavailable
from copilot_gateway.provider.prompt import AttachedFile, NormalizedMessage
from copilot_gateway.storage.store import SessionStore

FIXTURES = Path(__file__).parent / "fixtures"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def mock_base_url():
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def user(text: str, files: tuple[AttachedFile, ...] = ()) -> NormalizedMessage:
    return NormalizedMessage("user", text, files)


def assistant(text: str) -> NormalizedMessage:
    return NormalizedMessage("assistant", text)


def test_second_turn_reuses_the_thread(mock_base_url, settings_factory):
    settings = settings_factory(f"{mock_base_url}/mock_copilot.html")

    async def scenario():
        provider = CopilotProvider(settings, store=SessionStore(settings.sessions_path))
        await provider.start()
        try:
            first = await provider.complete("s1", [user("CODE hello")])
            second = await provider.complete("s1", [user("CODE hello"), assistant(first), user("again")])
            return first, second
        finally:
            await provider.stop()

    first, second = asyncio.run(scenario())

    assert first.startswith("turn 1:")
    # The last line of the multi-line prompt must arrive. A truncated send would stop at the first line.
    assert "last line: Reply only to the last USER message." in first
    # Turn 2 proves the thread was reused. A fresh thread would count only one turn.
    assert second.startswith("turn 2:")
    assert "last line: again" in second
    # Code fences survive extraction, so agents like Aider can still parse the reply.
    assert "```python\nprint(1)\n```" in first


def test_stateless_requests_share_one_thread_per_session(mock_base_url, settings_factory):
    # curl-style clients send only the newest message, with no history.
    settings = settings_factory(f"{mock_base_url}/mock_copilot.html")

    async def scenario():
        provider = CopilotProvider(settings, store=SessionStore(settings.sessions_path))
        await provider.start()
        try:
            first = await provider.complete("s2", [user("hello")])
            second = await provider.complete("s2", [user("again")])
            other_session = await provider.complete("s3", [user("hello")])
            return first, second, other_session
        finally:
            await provider.stop()

    first, second, other_session = asyncio.run(scenario())

    assert first.startswith("turn 1:")
    assert second.startswith("turn 2:")
    assert "last line: again" in second
    # A different session key gets its own thread.
    assert other_session.startswith("turn 1:")


def test_attached_files_reach_the_page(mock_base_url, settings_factory):
    settings = settings_factory(f"{mock_base_url}/mock_copilot.html")
    attachment = AttachedFile(filename="notes.py", data=b"print(1)\n")

    async def scenario():
        provider = CopilotProvider(settings, store=SessionStore(settings.sessions_path))
        await provider.start()
        try:
            return await provider.complete("s3", [user("review this", (attachment,))])
        finally:
            await provider.stop()

    reply = asyncio.run(scenario())

    assert "attachments: notes.py" in reply


def test_long_prompt_is_moved_into_an_attached_file(mock_base_url, settings_factory):
    settings = settings_factory(f"{mock_base_url}/mock_copilot.html", max_inline_chars=40)

    async def scenario():
        provider = CopilotProvider(settings, store=SessionStore(settings.sessions_path))
        await provider.start()
        try:
            return await provider.complete("s4", [user("a question that makes the prompt long")])
        finally:
            await provider.stop()

    reply = asyncio.run(scenario())

    assert "attachments: conversation.txt" in reply


def test_browser_replaces_a_closed_tab(settings_factory):
    settings = settings_factory()

    async def scenario():
        browser = BrowserSession(settings)
        await browser.start()
        try:
            page = await browser.page()
            await page.close()
            replacement = await browser.page()
            assert not replacement.is_closed()
            await browser.recover()
            after_recover = await browser.page()
            assert not after_recover.is_closed()
        finally:
            await browser.close()

    asyncio.run(scenario())


def test_missing_chat_box_reports_sign_in_needed(mock_base_url, settings_factory):
    settings = settings_factory(f"{mock_base_url}/empty.html", login_timeout=1)

    async def scenario():
        provider = CopilotProvider(settings, store=SessionStore(settings.sessions_path))
        await provider.start()
        try:
            await provider.complete("s5", [user("hello")])
        finally:
            await provider.stop()

    with pytest.raises(ProviderUnavailable, match="Sign in"):
        asyncio.run(scenario())
