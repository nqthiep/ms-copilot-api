"""Every DOM selector the gateway uses. Nothing else in the project hardcodes one.

These are best guesses. The live Copilot page could not be reached while this
was written, so verify each entry in a real session and update it here. On a
failed lookup the gateway saves a screenshot to storage/debug/ to help.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Selectors:
    chat_input: str = 'textarea[aria-label*="Message" i], [role="textbox"][aria-label*="Message" i]'
    send_button: str = 'button[aria-label*="Send" i]'
    stop_button: str = 'button[aria-label*="Stop" i]'
    response_item: str = '[data-content="ai-message"]'
    new_thread_button: str = 'button[aria-label*="New" i]'
    attach_button: str = 'button[aria-label*="Attach" i]'
    file_input: str = 'input[type="file"]'


SELECTORS = Selectors()
