import asyncio

import pytest

from copilot_gateway.provider.copilot_provider import CopilotProvider, EmptyResponse
from copilot_gateway.provider.errors import ProviderTimeout
from copilot_gateway.provider.prompt import NormalizedMessage
from copilot_gateway.storage.store import SessionStore


class _NoBrowser:
    """Stands in for BrowserSession. Attempts are replaced, so no real browser is needed."""

    async def recover(self) -> None:
        pass

    async def page(self):
        raise RuntimeError("no browser in this test")

    async def close(self) -> None:
        pass


class ScriptedProvider(CopilotProvider):
    def __init__(self, settings, fail_times: int = 0, hang: bool = False) -> None:
        super().__init__(settings, browser=_NoBrowser(), store=SessionStore(settings.sessions_path))
        self.fail_times = fail_times
        self.hang = hang
        self.attempts = 0
        self.recoveries = 0

    async def _attempt(self, session_key, messages, fresh_thread):
        self.attempts += 1
        if self.hang:
            await asyncio.sleep(10)
        if self.attempts <= self.fail_times:
            raise EmptyResponse("empty")
        return "ok"

    async def _recover(self) -> None:
        self.recoveries += 1
        self._dirty = False


MESSAGES = [NormalizedMessage("user", "hello")]


def test_empty_responses_are_retried_up_to_max_retry(settings_factory):
    provider = ScriptedProvider(settings_factory(max_retry=3), fail_times=2)

    reply = asyncio.run(provider.complete("s", MESSAGES))

    assert reply == "ok"
    assert provider.attempts == 3
    assert provider.recoveries == 2


def test_gives_up_after_the_retry_budget(settings_factory):
    provider = ScriptedProvider(settings_factory(max_retry=2), fail_times=10)

    with pytest.raises(EmptyResponse):
        asyncio.run(provider.complete("s", MESSAGES))

    assert provider.attempts == 3  # one attempt plus two retries


def test_request_timeout_bounds_the_whole_request(settings_factory):
    provider = ScriptedProvider(settings_factory(request_timeout=0.2), hang=True)

    with pytest.raises(ProviderTimeout):
        asyncio.run(provider.complete("s", MESSAGES))
