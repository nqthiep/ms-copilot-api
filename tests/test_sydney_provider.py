"""Tests for SydneyProvider with the WebSocket layer stubbed out."""

import asyncio
import base64
import json

import pytest

from copilot_gateway.provider import sydney_provider
from copilot_gateway.provider.base import TextDelta, ToolCallsEvent
from copilot_gateway.provider.prompt import AttachedImage, NormalizedMessage
from copilot_gateway.provider.sydney import EmptyResponse
from copilot_gateway.provider.sydney_provider import SydneyProvider
from copilot_gateway.storage.store import SessionStore


def fake_jwt():
    payload = base64.urlsafe_b64encode(json.dumps({"oid": "o", "tid": "t"}).encode()).rstrip(b"=").decode()
    return f"h.{payload}.s"


class FakeTokens:
    def __init__(self, token: str = "") -> None:
        self._token = token or fake_jwt()

    async def get_token(self, *, interactive: bool = True) -> str:
        return self._token

    def start_interactive_login(self):
        raise AssertionError("should not need interactive login in tests")


@pytest.fixture
def provider(settings_factory):
    settings = settings_factory(m365_access_token="unused")
    store = SessionStore(settings.sessions_path)
    return SydneyProvider(settings, store=store, tokens=FakeTokens())


def stub_stream(monkeypatch, script):
    """Replace stream_chat_turn with a scripted fake. Captures call kwargs.

    Each script entry is either an Exception (raised) or a list of text deltas.
    """
    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        behavior = script[len(calls) - 1]
        if isinstance(behavior, Exception):
            raise behavior
        for delta in behavior:
            yield delta

    monkeypatch.setattr(sydney_provider, "stream_chat_turn", fake)
    return calls


def run_events(provider, session_key, messages, **kwargs):
    async def collect():
        return [event async for event in provider.run(session_key, messages, **kwargs)]

    return asyncio.run(collect())


def test_first_turn_streams_text_and_persists_session(provider, monkeypatch):
    calls = stub_stream(monkeypatch, [["Hello", " world"]])
    messages = [NormalizedMessage(role="user", text="hi")]

    events = run_events(provider, "s1", messages)

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello", " world"]
    assert calls[0]["text"].startswith("USER") or "hi" in calls[0]["text"]
    assert calls[0]["tone"] == "magic"
    assert calls[0]["conversation"].is_first_turn is True
    state = provider._store.get("s1")
    assert state is not None
    assert state.sydney_conversation_id
    assert state.sydney_turns == 1


def test_second_turn_sends_only_pending_messages(provider, monkeypatch):
    calls = stub_stream(monkeypatch, [["one"], ["two"]])
    first = [NormalizedMessage(role="user", text="first question")]
    run_events(provider, "s1", first)

    second = first + [
        NormalizedMessage(role="assistant", text="one"),
        NormalizedMessage(role="user", text="second question"),
    ]
    run_events(provider, "s1", second)

    assert "first question" not in calls[1]["text"]
    assert "second question" in calls[1]["text"]
    assert calls[1]["conversation"].is_first_turn is False
    assert calls[1]["conversation"].conversation_id == calls[0]["conversation"].conversation_id
    assert provider._store.get("s1").sydney_turns == 2


def test_tool_calls_are_parsed_and_emitted(provider, monkeypatch):
    from copilot_gateway.models.request import ToolDef

    tools = [
        ToolDef(
            function={"name": "read_file", "description": "Read", "parameters": {}},
        )
    ]
    reply = (
        'On it.\n```tool_calls\n[{"name": "read_file", "arguments": {"path": "a.py"}}]\n```'
    )
    stub_stream(monkeypatch, [[reply]])
    messages = [NormalizedMessage(role="user", text="read a.py")]

    events = run_events(provider, "s1", messages, tools=tools, tool_choice="auto")

    texts = [e.text for e in events if isinstance(e, TextDelta)]
    tool_events = [e for e in events if isinstance(e, ToolCallsEvent)]
    # The fence is parsed and hidden; surrounding prose stays as the content.
    assert texts == ["On it."]
    assert len(tool_events) == 1
    (call,) = tool_events[0].calls
    assert call.name == "read_file"
    assert json.loads(call.arguments) == {"path": "a.py"}
    assert call.id.startswith("call_")


def test_no_tools_means_no_tool_parsing(provider, monkeypatch):
    reply = "```tool_calls\n[]\n```\nJust text."
    stub_stream(monkeypatch, [[reply]])

    events = run_events(provider, "s1", [NormalizedMessage(role="user", text="hi")])

    assert not [e for e in events if isinstance(e, ToolCallsEvent)]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == reply


def test_tool_choice_none_disables_preamble(provider, monkeypatch):
    calls = stub_stream(monkeypatch, [["ok"]])
    tools = [{"type": "function", "function": {"name": "f", "description": "d"}}]

    run_events(
        provider,
        "s1",
        [NormalizedMessage(role="user", text="hi")],
        tools=tools,
        tool_choice="none",
    )

    assert "```tool_calls" not in calls[0]["text"]


def test_empty_response_retries_on_fresh_conversation(provider, monkeypatch):
    calls = stub_stream(monkeypatch, [EmptyResponse("empty"), ["recovered"]])

    events = run_events(provider, "s1", [NormalizedMessage(role="user", text="hi")])

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["recovered"]
    assert len(calls) == 2
    # The retry starts a brand-new server-side conversation.
    assert calls[1]["conversation"].conversation_id != calls[0]["conversation"].conversation_id
    assert calls[1]["conversation"].is_first_turn is True


def test_images_are_uploaded_and_annotated(provider, monkeypatch):
    from copilot_gateway.provider import sydney as sydney_module

    async def fake_upload(http, **kwargs):
        return sydney_module.UploadedImage(doc_id="doc-9", file_type="png", file_name="x.png")

    monkeypatch.setattr(sydney_provider, "upload_image", fake_upload)
    calls = stub_stream(monkeypatch, [["seen"]])
    messages = [
        NormalizedMessage(
            role="user",
            text="what is this",
            images=(AttachedImage(filename="x.png", data=b"123", mime="image/png"),),
        )
    ]

    run_events(provider, "s1", messages)

    assert calls[0]["images"][0].doc_id == "doc-9"


def test_model_selects_tone(provider, monkeypatch):
    calls = stub_stream(monkeypatch, [["ok"]])

    run_events(provider, "s1", [NormalizedMessage(role="user", text="hi")], model="claude-sonnet")

    assert calls[0]["tone"] == "Claude_Sonnet"


def test_complete_joins_deltas(provider, monkeypatch):
    stub_stream(monkeypatch, [["a", "b", "c"]])

    async def scenario():
        return await provider.complete("s1", [NormalizedMessage(role="user", text="hi")])

    assert asyncio.run(scenario()) == "abc"
