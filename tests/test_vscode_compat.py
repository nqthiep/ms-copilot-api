"""API-level checks for the shapes the VSCode Copilot Chat extension sends."""

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletion as OpenAIChatCompletion
from openai.types.chat import ChatCompletionChunk as OpenAIChatCompletionChunk

from copilot_gateway.app import create_app
from copilot_gateway.models.request import ToolDef
from copilot_gateway.provider.base import (
    ChatProvider,
    ProviderEvent,
    TextDelta,
    ToolCall,
    ToolCallsEvent,
)
from copilot_gateway.provider.prompt import NormalizedMessage


class StreamingProvider(ChatProvider):
    """A provider double that replays a script of events."""

    def __init__(self, script: list[ProviderEvent]) -> None:
        self.script = script
        self.runs: list[dict] = []

    async def complete(self, session_key: str, messages: list[NormalizedMessage]) -> str:
        return "".join(e.text for e in self.script if isinstance(e, TextDelta))

    async def run(
        self,
        session_key: str,
        messages: list[NormalizedMessage],
        *,
        tools: list[ToolDef] | None = None,
        tool_choice: object = None,
        model: str = "m365-copilot",
    ) -> AsyncIterator[ProviderEvent]:
        self.runs.append({"tools": tools, "tool_choice": tool_choice, "model": model})
        for event in self.script:
            yield event


def tool_call_event() -> ToolCallsEvent:
    return ToolCallsEvent(
        calls=(
            ToolCall(id="call_1", name="read_file", arguments='{"path": "a.py"}'),
        )
    )


def tools_body():
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]


@pytest.fixture
def tool_client(settings_factory):
    provider = StreamingProvider([TextDelta("Working on it. "), tool_call_event()])
    app = create_app(settings=settings_factory(), provider=provider)
    with TestClient(app) as client:
        yield client, provider


def test_non_streaming_tool_calls_shape(tool_client):
    client, provider = tool_client

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "m365-copilot",
            "messages": [{"role": "user", "content": "read a.py"}],
            "tools": tools_body(),
            "tool_choice": "auto",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    (tool_call,) = body["choices"][0]["message"]["tool_calls"]
    assert tool_call["id"] == "call_1"
    assert tool_call["type"] == "function"
    assert tool_call["function"] == {"name": "read_file", "arguments": '{"path": "a.py"}'}
    assert len(provider.runs[0]["tools"]) == 1

    parsed = OpenAIChatCompletion.model_validate(body)
    assert parsed.choices[0].message.tool_calls[0].function.name == "read_file"


def test_streaming_tool_calls_sse(tool_client):
    client, _ = tool_client

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "m365-copilot",
            "messages": [{"role": "user", "content": "read a.py"}],
            "tools": tools_body(),
            "tool_choice": "auto",
            "stream": True,
        },
    ) as response:
        lines = [line for line in response.iter_lines() if line.startswith("data: ")]

    assert lines[-1] == "data: [DONE]"
    payloads = [
        json.loads(line.removeprefix("data: ")) for line in lines[:-1]
    ]
    for payload in payloads:
        OpenAIChatCompletionChunk.model_validate(payload)

    tool_deltas = [
        p for p in payloads if p["choices"][0]["delta"].get("tool_calls")
    ]
    assert len(tool_deltas) == 1
    (call,) = tool_deltas[0]["choices"][0]["delta"]["tool_calls"]
    assert call["id"] == "call_1"
    assert call["function"]["name"] == "read_file"
    assert payloads[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_tool_messages_in_history_are_accepted(settings_factory):
    provider = StreamingProvider([TextDelta("done")])
    app = create_app(settings=settings_factory(), provider=provider)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "m365-copilot",
                "messages": [
                    {"role": "user", "content": "read a.py"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "a.py"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_1",
                        "name": "read_file",
                        "content": "print('hi')",
                    },
                ],
                "tools": tools_body(),
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == "stop"


def test_stream_options_include_usage(settings_factory):
    provider = StreamingProvider([TextDelta("hi")])
    app = create_app(settings=settings_factory(), provider=provider)
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "m365-copilot",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        ) as response:
            payloads = [
                json.loads(line.removeprefix("data: "))
                for line in response.iter_lines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]

    assert payloads[-1]["usage"]["total_tokens"] >= 2


def test_extra_vscode_fields_are_ignored(settings_factory):
    provider = StreamingProvider([TextDelta("ok")])
    app = create_app(settings=settings_factory(), provider=provider)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "m365-copilot",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "hi"},
                        ],
                    }
                ],
                "temperature": 0.2,
                "top_p": 0.9,
                "max_completion_tokens": 100,
                "presence_penalty": 0,
                "frequency_penalty": 0,
                "seed": 7,
                "stop": ["\n\n"],
                "n": 1,
                "logit_bias": {"1": -100},
                "response_format": {"type": "text"},
                "parallel_tool_calls": False,
                "reasoning_effort": "medium",
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ok"


def test_chat_completions_alias_route(settings_factory):
    provider = StreamingProvider([TextDelta("ok")])
    app = create_app(settings=settings_factory(), provider=provider)
    with TestClient(app) as client:
        response = client.post(
            "/chat/completions",
            json={"model": "m365-copilot", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert response.json()["object"] == "chat.completion"


def test_auth_error_maps_to_401(settings_factory):
    from copilot_gateway.provider.errors import AuthRequired

    class Denied(ChatProvider):
        async def complete(self, session_key, messages):
            raise AuthRequired("sign in again")

    app = create_app(settings=settings_factory(), provider=Denied())
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "m365-copilot", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"
