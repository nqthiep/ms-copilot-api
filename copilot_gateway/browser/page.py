import asyncio
from pathlib import Path

from playwright.async_api import Page

from copilot_gateway.browser.selectors import SELECTORS, Selectors
from copilot_gateway.config import Settings

NAVIGATION_TIMEOUT_MS = 30_000
POLL_SECONDS = 0.5

# Renders a response element as plain text. Code blocks keep their ``` fences,
# because coding agents such as Aider parse edits from fenced blocks.
RENDER_JS = r"""
(root) => {
  const BLOCK = new Set(["P", "DIV", "UL", "OL", "LI", "H1", "H2", "H3", "H4", "H5", "H6", "BLOCKQUOTE", "TABLE", "TR"]);
  const render = (node) => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    if (node.tagName === "PRE") {
      const code = node.querySelector("code") || node;
      const lang = (code.className.match(/language-(\S+)/) || [])[1] || "";
      return "\n" + "```" + lang + "\n" + code.textContent.replace(/\n$/, "") + "\n" + "```" + "\n";
    }
    if (node.tagName === "BR") return "\n";
    if (node.tagName === "BUTTON" || node.tagName === "SVG") return "";
    const inner = Array.from(node.childNodes).map(render).join("");
    return BLOCK.has(node.tagName) ? "\n" + inner + "\n" : inner;
  };
  return render(root).replace(/\n{3,}/g, "\n\n").trim();
}
"""


class CopilotPage:
    """Operations on one Copilot tab. Each method maps to a step in the request flow."""

    def __init__(self, page: Page, settings: Settings, selectors: Selectors = SELECTORS) -> None:
        self._page = page
        self._settings = settings
        self._sel = selectors

    @property
    def url(self) -> str:
        return self._page.url

    async def open_home(self, timeout_ms: int) -> None:
        await self._page.goto(self._settings.copilot_url, wait_until="domcontentloaded")
        await self.wait_ready(timeout_ms)

    async def wait_ready(self, timeout_ms: int) -> None:
        await self._page.locator(self._sel.chat_input).first.wait_for(
            state="visible", timeout=timeout_ms
        )

    async def create_thread(self) -> None:
        button = self._page.locator(self._sel.new_thread_button)
        if await button.count():
            await button.first.click()
        else:
            # Already on a fresh chat, so there is no "New chat" button to press.
            await self._page.goto(self._settings.copilot_url, wait_until="domcontentloaded")
        await self.wait_ready(NAVIGATION_TIMEOUT_MS)

    async def open_thread(self, thread_url: str) -> None:
        await self._page.goto(thread_url, wait_until="domcontentloaded")
        await self.wait_ready(NAVIGATION_TIMEOUT_MS)

    async def attach_files(self, paths: list[Path]) -> None:
        for path in paths:
            if await self._page.locator(self._sel.file_input).count() == 0:
                await self._page.locator(self._sel.attach_button).first.click()
            await self._page.locator(self._sel.file_input).first.set_input_files(str(path))
            # Wait until the UI shows the file, so the message is not sent before the upload.
            await self._page.get_by_text(path.name).first.wait_for(timeout=NAVIGATION_TIMEOUT_MS)

    async def send_message(self, text: str) -> int:
        """Type and submit a message. Returns how many response items existed before sending."""
        baseline = await self._page.locator(self._sel.response_item).count()
        box = self._page.locator(self._sel.chat_input).first
        await box.fill(text)
        send = self._page.locator(self._sel.send_button)
        if await send.count():
            await send.first.click()
        else:
            # The send button may only render once the box has text. Enter submits in Copilot.
            await box.press("Enter")
        return baseline

    async def read_response(self, baseline: int) -> str:
        """Wait until a new response has finished streaming, then return its text.

        Finished means the stop button is gone and the text has not changed for
        RESPONSE_STABLE_SECONDS. The caller bounds the wait with a timeout.
        """
        items = self._page.locator(self._sel.response_item)
        stop = self._page.locator(self._sel.stop_button)
        loop = asyncio.get_running_loop()
        last_text: str | None = None
        stable_since = 0.0

        while True:
            count = await items.count()
            if count > baseline:
                text = await items.nth(count - 1).evaluate(RENDER_JS)
                generating = await stop.count() > 0
                now = loop.time()
                if text != last_text:
                    last_text, stable_since = text, now
                elif not generating and now - stable_since >= self._settings.stable_seconds:
                    return text
            await asyncio.sleep(POLL_SECONDS)
