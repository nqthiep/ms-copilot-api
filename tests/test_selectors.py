"""Checks selectors against markup copied from the live Copilot page (not the mock)."""

import asyncio

from playwright.async_api import async_playwright

from copilot_gateway.browser.selectors import SELECTORS

# Copied from the real page's DevTools output: the chat box is a SPAN, not a div or textarea.
# The span needs a size, or Playwright treats it as not visible, as a real composer would be.
LIVE_CHAT_BOX = (
    '<div id="composer"><span role="textbox" aria-label="Message Copilot" '
    'contenteditable="true" style="display:block;width:300px;height:24px"></span></div>'
)


def test_chat_input_selector_matches_live_markup(settings_factory):
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                executable_path=settings_factory().chromium_path,
            )
            try:
                page = await browser.new_page()
                await page.set_content(LIVE_CHAT_BOX)
                box = page.locator(SELECTORS.chat_input).first
                await box.fill("hello")
                return await box.text_content()
            finally:
                await browser.close()

    assert asyncio.run(scenario()) == "hello"
