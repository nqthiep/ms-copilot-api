"""Every DOM selector the browser provider uses. Nothing else hardcodes one.

The composer selectors below were confirmed against the live
copilot.cloud.microsoft/chat page (a Lexical contenteditable composer). The
response-item selector is still a candidate: verify it in a real session. On a
failed lookup the gateway saves a screenshot to storage/debug/ to help.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Selectors:
    # The Lexical composer: <div contenteditable="true" role="textbox"
    # id="m365-chat-editor-target-element">.
    chat_input: str = (
        '#m365-chat-editor-target-element, '
        '[contenteditable="true"][role="textbox"], '
        'textarea[aria-label*="Message" i]'
    )
    send_button: str = 'button[aria-label*="Send" i]'
    stop_button: str = 'button[aria-label*="Stop" i], button[aria-label*="Dừng" i]'
    response_item: str = '[data-testid="copilot-message-reply-div"]'  # candidate, not yet confirmed
    new_thread_button: str = 'a[aria-label*="New chat" i], button[aria-label*="New chat" i]'
    attach_button: str = 'button[data-testid="PlusMenuButton"], button[aria-label*="Attach" i]'
    file_input: str = 'input[type="file"]'
    # Accessible name of the control shown when signed out. Matched by role and name, not CSS.
    sign_in_text: str = "Sign in"


SELECTORS = Selectors()
