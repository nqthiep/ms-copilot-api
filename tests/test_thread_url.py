from copilot_gateway.provider.copilot_provider import _thread_url_after_turn
from copilot_gateway.storage.store import ThreadState


def test_new_chat_whose_url_changed_is_stored():
    assert _thread_url_after_turn("new-thread", None, "https://x/chat?fromcode=1", "https://x/chat/abc") == "https://x/chat/abc"


def test_new_chat_whose_url_never_changed_is_not_stored():
    url = "https://x/chat?fromcode=1"

    assert _thread_url_after_turn("new-thread", None, url, url) is None


def test_continued_thread_keeps_its_stored_url():
    state = ThreadState(thread_url="https://x/chat/abc", message_count=3, prefix_hash="h")

    assert _thread_url_after_turn("continue", state, "https://x/chat/abc", "https://x/chat/abc") == "https://x/chat/abc"
