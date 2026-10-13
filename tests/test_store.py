from copilot_gateway.storage.store import SessionStore, ThreadState


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "sessions.json"
    state = ThreadState(thread_url="https://example.test/chat/1", user_turns=2)

    SessionStore(path).put("auto:1", state)

    assert SessionStore(path).get("auto:1") == state


def test_forget_removes_the_mapping(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    store.put("auto:1", ThreadState(thread_url=None, user_turns=1))

    store.forget("auto:1")

    assert store.get("auto:1") is None


def test_entry_from_an_older_format_is_ignored(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text('{"auto:1": {"thread_url": "x", "message_count": 2, "prefix_hash": "h"}}', encoding="utf-8")

    assert SessionStore(path).get("auto:1") is None


def test_unreadable_file_starts_empty_instead_of_crashing(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{not json", encoding="utf-8")

    assert SessionStore(path).get("anything") is None
