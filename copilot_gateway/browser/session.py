from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from copilot_gateway.config import Settings
from copilot_gateway.utils.logging import get_logger

log = get_logger("browser")


class BrowserSession:
    """Owns one persistent Chromium profile, so cookies and sign-in survive restarts."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        await self._launch()

    async def page(self) -> Page:
        """Return a usable page, opening a new tab if the previous one was closed or crashed."""
        if self._context is None:
            await self._launch()
        if self._page is None or self._page.is_closed():
            log.info("Opening a new browser tab")
            self._page = await self._context.new_page()
        return self._page

    async def recover(self) -> None:
        """Drop the current tab and context, then relaunch on the same profile."""
        log.warning("Recovering browser session")
        await self._close_context()
        await self._launch()

    async def close(self) -> None:
        await self._close_context()
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _launch(self) -> None:
        assert self._playwright is not None, "start() must be called first"
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._settings.profile_dir),
            headless=self._settings.headless,
            executable_path=self._settings.chromium_path,
        )
        self._page = None

    async def _close_context(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # the browser may already be dead; nothing to salvage
                log.debug("Ignoring error while closing a dead browser context", exc_info=True)
        self._context = None
        self._page = None
