import base64
import json

import pytest
from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletion as OpenAIChatCompletion
from openai.types.chat import ChatCompletionChunk as OpenAIChatCompletionChunk

from copilot_gateway.app import create_app
from copilot_gateway.provider.base import ChatProvider
from copilot_gateway.provider.errors import ProviderTimeout
from copilot_gateway.provider.prompt import NormalizedMessage


class FakeProvider(ChatProvider):
    def __init__(self, reply: str = "pong", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[tuple[str, list[NormalizedMessage]]] = []

    async def complete(self, session_key, messages):
        self.calls.append((session_key, messages))
        if self.error is not None:
            raise self.error
        return self.reply


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def client(settings_factory, provider):
    app = create_app(settings=settings_factory(), provider=provider)
    with TestClient(app) as test_client:
        yield test_client


def chat(client, headers=None, **body):
    payload = {"model": "copilot-web", "messages": [{"role": "user", "content": "hello"}]}
    payload.update(body)
    return client.post("/v1/chat/completions", json=payload, headers=headers)


def test_health_reports_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_models_lists_copilot_web(client):
    response = client.get("/v1/models")

    assert response.json()["data"][0]["id"] == "copilot-web"


def test_chat_completion_has_openai_shape(client, provider):
    response = chat(client)
    body = response.json()

    assert response.status_code == 200
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "pong"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] >= 2
    assert provider.calls[0][1][0].text == "hello"


def test_stream_returns_sse_chunks_and_done(client):
    with client.stream("POST", "/v1/chat/completions", json={
        "model": "copilot-web",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }) as response:
        lines = [line for line in response.iter_lines() if line.startswith("data: ")]

    assert response.headers["content-type"].startswith("text/event-stream")
    assert lines[-1] == "data: [DONE]"
    first = json.loads(lines[0].removeprefix("data: "))
    assert first["object"] == "chat.completion.chunk"
    assert first["choices"][0]["delta"]["content"] == "pong"


def test_image_parts_are_not_supported(client):
    response = chat(client, messages=[{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
    ]}])

    assert response.status_code == 422


def test_disallowed_attachment_returns_400(client):
    response = chat(client, messages=[{"role": "user", "content": [
        {"type": "file", "file": {"filename": "tool.exe", "file_data": "AAAA"}},
    ]}])

    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["error"]["message"]


def test_request_without_user_message_returns_400(client):
    response = chat(client, messages=[{"role": "system", "content": "rules"}])

    assert response.status_code == 400


def test_provider_timeout_maps_to_504(settings_factory):
    provider = FakeProvider(error=ProviderTimeout("slow"))
    app = create_app(settings=settings_factory(), provider=provider)
    with TestClient(app) as client:
        response = chat(client)

    assert response.status_code == 504
    assert response.json()["error"]["type"] == "timeout"


def test_session_header_gives_explicit_key(client, provider):
    chat(client, headers={"X-Session-Id": "agent-1"})

    assert provider.calls[0][0] == "explicit:agent-1"


def test_auto_session_key_is_stable_across_turns(client, provider):
    chat(client)
    chat(client, messages=[
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "pong"},
        {"role": "user", "content": "next question"},
    ])
    chat(client, messages=[{"role": "user", "content": "a different conversation"}])

    first_key, second_key, other_key = (call[0] for call in provider.calls)
    assert first_key == second_key
    assert first_key.startswith("auto:")
    assert other_key != first_key


def test_file_part_reaches_the_provider(client, provider):
    encoded = base64.b64encode(b"print('hi')\n").decode()

    response = chat(client, messages=[{"role": "user", "content": [
        {"type": "text", "text": "summarize"},
        {"type": "file", "file": {"filename": "main.py", "file_data": f"data:text/x-python;base64,{encoded}"}},
    ]}])

    assert response.status_code == 200
    (message,) = provider.calls[0][1]
    assert message.text == "summarize"
    assert message.files[0].filename == "main.py"
    assert message.files[0].data == b"print('hi')\n"


def test_response_matches_the_openai_sdk_schema(client):
    body = chat(client).json()

    parsed = OpenAIChatCompletion.model_validate(body)

    assert parsed.object == "chat.completion"
    assert parsed.choices[0].message.role == "assistant"
    assert parsed.choices[0].message.content == "pong"


def test_stream_chunks_match_the_openai_sdk_schema(client):
    with client.stream("POST", "/v1/chat/completions", json={
        "model": "copilot-web",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }) as response:
        payloads = [
            line.removeprefix("data: ")
            for line in response.iter_lines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]

    chunks = [OpenAIChatCompletionChunk.model_validate_json(payload) for payload in payloads]

    assert chunks[0].choices[0].delta.content == "pong"
    assert chunks[-1].choices[0].finish_reason == "stop"
