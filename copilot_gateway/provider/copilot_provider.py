import asyncio
import tempfile
import time
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from copilot_gateway.browser.page import CopilotPage
from copilot_gateway.browser.session import BrowserSession
from copilot_gateway.config import Settings
from copilot_gateway.provider.base import ChatProvider
from copilot_gateway.provider.errors import ProviderTimeout, ProviderUnavailable
from copilot_gateway.provider.prompt import (
    AttachedFile,
    NormalizedMessage,
    compile_prompt,
    format_user_turns,
    pending_user_turns,
)
from copilot_gateway.storage.store import SessionStore, ThreadState
from copilot_gateway.utils.logging import get_logger

log = get_logger("provider")

CONVERSATION_FILE = "conversation.txt"
OVERFLOW_PROMPT = (
    f"The full conversation is in the attached file {CONVERSATION_FILE}. "
    "Reply only to its last USER message."
)


class EmptyResponse(Exception):
    """Copilot finished without producing any text."""


# Failures worth another attempt on a fresh thread. Playwright's TimeoutError is a subclass.
RETRIABLE = (EmptyResponse, PlaywrightError)


class CopilotProvider(ChatProvider):
    """Runs each chat turn in a Copilot thread, one request at a time on a single tab."""

    def __init__(
        self,
        settings: Settings,
        browser: BrowserSession | None = None,
        store: SessionStore | None = None,
    ) -> None:
        self._settings = settings
        self._browser = browser or BrowserSession(settings)
        self._store = store or SessionStore(settings.sessions_path)
        self._lock = asyncio.Lock()
        self._home_ready = False
        self._dirty = False  # set after a failure; the next request recovers the browser first
        self._warmup: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._browser.start()
        self._warmup = asyncio.create_task(self._warm_up())

    async def stop(self) -> None:
        if self._warmup is not None:
            self._warmup.cancel()
        await self._browser.close()

    def health(self) -> dict[str, object]:
        return {"browser_ready": self._home_ready}

    async def complete(self, session_key: str, messages: list[NormalizedMessage]) -> str:
        async with self._lock:
            try:
                return await asyncio.wait_for(
                    self._complete_with_retries(session_key, messages),
                    timeout=self._settings.request_timeout,
                )
            except TimeoutError as exc:
                self._dirty = True
                await self._capture_debug("timeout")
                raise ProviderTimeout(
                    f"Copilot did not answer within {self._settings.request_timeout:.0f}s"
                ) from exc

    async def _warm_up(self) -> None:
        async with self._lock:
            try:
                await self._ensure_home()
            except Exception as exc:  # the first request will retry and report the error
                log.warning("Copilot is not ready yet: %s", exc)

    async def _complete_with_retries(
        self, session_key: str, messages: list[NormalizedMessage]
    ) -> str:
        attempts = self._settings.max_retry + 1
        for attempt in range(1, attempts + 1):
            try:
                if self._dirty:
                    await self._recover()
                return await self._attempt(session_key, messages, fresh_thread=attempt > 1)
            except Exception as exc:
                self._dirty = True
                self._store.forget(session_key)
                retriable = isinstance(exc, RETRIABLE)
                if not retriable or attempt == attempts:
                    await self._capture_debug("failure")
                    raise
                log.warning(
                    "Attempt %d/%d failed (%s); retrying on a new thread",
                    attempt,
                    attempts,
                    type(exc).__name__,
                )
        raise AssertionError("unreachable")

    async def _attempt(
        self, session_key: str, messages: list[NormalizedMessage], fresh_thread: bool
    ) -> str:
        await self._ensure_home()
        copilot = CopilotPage(await self._browser.page(), self._settings)

        # The session key decides the thread. Only turns the thread has not seen are sent.
        state = None if fresh_thread else self._store.get(session_key)
        if state is not None and state.thread_url is not None:
            if copilot.url != state.thread_url:
                await copilot.open_thread(state.thread_url)
            turns = pending_user_turns(messages, state.user_turns)
            turn_text = format_user_turns(turns)
            sent_before = state.user_turns
            mode = "continue"
        else:
            await copilot.create_thread()
            turns = [m for m in messages if m.role == "user"]
            turn_text = compile_prompt(messages)
            sent_before = 0
            mode = "new-thread"
        turn_files = _files_of(turns)

        with tempfile.TemporaryDirectory(prefix="copilot-gw-") as tmp:
            paths = _write_files(Path(tmp), turn_files)
            if len(turn_text) > self._settings.max_inline_chars:
                overflow = Path(tmp) / CONVERSATION_FILE
                overflow.write_text(turn_text, encoding="utf-8")
                paths.append(overflow)
                turn_text = OVERFLOW_PROMPT

            log.info(
                "session=%s mode=%s prompt_chars=%d attachments=%d",
                session_key,
                mode,
                len(turn_text),
                len(paths),
            )
            if paths:
                await copilot.attach_files(paths)
            url_before_send = copilot.url
            baseline = await copilot.send_message(turn_text)
            reply = await copilot.read_response(baseline)

        if not reply.strip():
            raise EmptyResponse("Copilot returned an empty response")

        self._store.put(
            session_key,
            ThreadState(
                thread_url=_thread_url_after_turn(mode, state, url_before_send, copilot.url),
                user_turns=sent_before + len(turns),
            ),
        )
        self._dirty = False
        return reply

    async def _ensure_home(self) -> None:
        if self._home_ready:
            return
        copilot = CopilotPage(await self._browser.page(), self._settings)
        try:
            await copilot.open_home(timeout_ms=int(self._settings.login_timeout * 1000))
        except PlaywrightTimeoutError as exc:
            raise ProviderUnavailable(
                "Copilot chat box not found. Sign in in the browser window the gateway opened."
            ) from exc
        self._home_ready = True
        log.info("Copilot page ready")

    async def _recover(self) -> None:
        await self._browser.recover()
        self._home_ready = False
        self._dirty = False

    async def _capture_debug(self, label: str) -> None:
        """Save a screenshot so a broken selector can be diagnosed. Best effort."""
        try:
            page = await self._browser.page()
            self._settings.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self._settings.debug_dir / f"{label}-{int(time.time())}.png"
            await page.screenshot(path=str(path))
            log.info("Saved debug screenshot to %s", path)
        except Exception:
            log.debug("Could not save debug screenshot", exc_info=True)


def _files_of(messages: list[NormalizedMessage]) -> list[AttachedFile]:
    return [f for m in messages for f in m.files]


def _thread_url_after_turn(
    mode: str, state: ThreadState | None, url_before: str, url_after: str
) -> str | None:
    """A continued thread keeps its URL. A new chat has one only if its URL changed during the turn.

    Copilot's URL before the first message may not name the thread, so a URL that
    never changes is not stored. The next turn then sends the full prompt, which is slower but correct.
    """
    if mode == "continue" and state is not None:
        return state.thread_url
    return url_after if url_after != url_before else None


def _write_files(root: Path, files: list[AttachedFile]) -> list[Path]:
    # One directory per file, so two uploads with the same name do not overwrite each other.
    paths: list[Path] = []
    for index, file in enumerate(files):
        target_dir = root / str(index)
        target_dir.mkdir()
        path = target_dir / file.filename
        path.write_bytes(file.data)
        paths.append(path)
    return paths
