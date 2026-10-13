"""Checks selectors against markup copied from the live Copilot page (not the mock)."""

import asyncio

import pytest
from playwright.async_api import async_playwright

from copilot_gateway.browser.page import CopilotPage, NewChatNotFound
from copilot_gateway.browser.selectors import SELECTORS

# Copied from the live page: New chat is a link, not a button.
LIVE_NEW_CHAT_LINK = '<nav><a href="/chat" aria-label="New chat">New chat</a></nav>'

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


def _in_page(markup, action, settings):
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, executable_path=settings.chromium_path)
            try:
                page = await browser.new_page()
                await page.set_content(markup)
                return await action(page)
            finally:
                await browser.close()

    return asyncio.run(scenario())


def test_new_chat_selector_matches_live_link(settings_factory):
    settings = settings_factory()

    count = _in_page(LIVE_NEW_CHAT_LINK, lambda page: page.locator(SELECTORS.new_thread_button).count(), settings)

    assert count == 1


def test_missing_new_chat_control_fails_instead_of_reusing_a_conversation(settings_factory):
    settings = settings_factory()

    async def action(page):
        with pytest.raises(NewChatNotFound):
            await CopilotPage(page, settings).create_thread()

    _in_page("<div>no new chat control here</div>", action, settings)
