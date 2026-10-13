"""Unit tests for the Sydney wire protocol helpers (no network)."""

import asyncio
import base64
import json
import threading
from urllib.parse import parse_qsl, urlparse

import pytest

from copilot_gateway.provider import sydney
from copilot_gateway.provider.m365_auth import (
    build_authorize_url,
    decode_jwt_payload,
    generate_pkce_pair,
)
from copilot_gateway.provider.sydney import (
    SydneyConversation,
    build_chat_invocation,
    build_metrics_frame,
    build_ws_url,
    fold_text,
    image_annotations,
    split_frames,
    tone_for_model,
)


def test_split_frames_handles_multiple_frames_per_message():
    raw = '{"protocol":"json","version":1}\x1e{"type":6}\x1e'

    assert split_frames(raw) == ['{"protocol":"json","version":1}', '{"type":6}']
    assert split_frames("") == []
    assert split_frames("\x1e") == []


def test_fold_text_emits_only_true_prefixes():
    answer, emit = fold_text("", "Hello")
    assert (answer, emit) == ("Hello", "Hello")

    answer, emit = fold_text(answer, "Hello world")
    assert (answer, emit) == ("Hello world", " world")

    # A full snapshot that restates the answer emits nothing new.
    answer, emit = fold_text(answer, "Hello world")
    assert (answer, emit) == ("Hello world", None)

    # A shorter snapshot is stale, not a retraction.
    answer, emit = fold_text(answer, "Hello")
    assert (answer, emit) == ("Hello world", None)

    # A divergent but longer snapshot replaces the answer but emits nothing
    # (already-emitted bytes cannot be retracted).
    answer, emit = fold_text(answer, "Hi there, friend")
    assert (answer, emit) == ("Hi there, friend", None)


def test_fold_text_first_token_as_snapshot_only():
    # The first token often arrives only as a snapshot; folding must not drop it.
    answer, emit = fold_text("", "The")
    assert emit == "The"
    answer, emit = fold_text(answer, "The quick")
    assert emit == " quick"


def test_build_ws_url_carries_token_and_ids():
    url = build_ws_url(
        oid="oid-1",
        tid="tid-1",
        session_id="sid",
        conversation_id="cid",
        token="tok",
        request_id="req",
    )

    assert url.startswith("wss://substrate.office.com/m365Copilot/Chathub/oid-1@tid-1?")
    query = dict(parse_qsl(urlparse(url).query))
    assert query["access_token"] == "tok"
    assert query["ConversationId"] == "cid"
    assert query["X-SessionId"] == "sid"
    assert query["source"] == '"officeweb"'
    assert query["scenario"] == "OfficeWebIncludedCopilot"
    assert "variants" in query


def test_build_chat_invocation_shape():
    invocation = build_chat_invocation(
        text="hi",
        tone="magic",
        request_id="req",
        session_id="sid",
        is_first_turn=True,
    )

    assert invocation["type"] == 4
    assert invocation["target"] == "chat"
    assert invocation["invocationId"] == "0"
    args = invocation["arguments"][0]
    assert args["message"]["text"] == "hi"
    assert args["message"]["author"] == "user"
    assert args["tone"] == "magic"
    assert args["isStartOfSession"] is True
    assert args["plugins"] == [{"Id": "BingWebSearch", "Source": "BuiltIn"}]
    assert "messageAnnotations" not in args["message"]


def test_build_chat_invocation_with_images_adds_annotations():
    invocation = build_chat_invocation(
        text="what is this",
        tone="magic",
        request_id="req",
        session_id="sid",
        is_first_turn=False,
        annotations=[{"id": "doc-1"}],
    )

    args = invocation["arguments"][0]
    assert args["isStartOfSession"] is False
    assert args["message"]["messageAnnotations"] == [{"id": "doc-1"}]
    assert "cwc_flux_image" in args["optionsSets"]


def test_build_metrics_frame_shape():
    frame = build_metrics_frame()

    assert frame["type"] == 1
    assert frame["target"] == "Metrics"
    assert "Timestamps" in frame["arguments"][0]


def test_tone_for_model():
    assert tone_for_model("m365-copilot") == "magic"
    assert tone_for_model("copilot-web") == "magic"
    assert tone_for_model("m365-quick") == "Gpt_5_5_Chat"
    assert tone_for_model("m365-think-deeper") == "Gpt_5_5_Reasoning"
    assert tone_for_model("claude-sonnet") == "Claude_Sonnet"
    assert tone_for_model("something-new", default="magic") == "magic"


def test_image_annotations_shape():
    annotations = image_annotations(
        [sydney.UploadedImage(doc_id="d1", file_type="png", file_name="a.png")]
    )

    assert annotations == [
        {
            "id": "d1",
            "messageAnnotationMetadata": {
                "@type": "File",
                "annotationType": "File",
                "fileType": "png",
                "fileName": "a.png",
            },
            "messageAnnotationType": "ImageFile",
        }
    ]


def test_sydney_conversation_first_turn_semantics():
    fresh = SydneyConversation()
    assert fresh.is_first_turn is True
    assert fresh.session_id and fresh.conversation_id

    resumed = SydneyConversation(
        session_id="s", conversation_id="c", turn_count=3, resumed=True
    )
    assert resumed.is_first_turn is False


def test_pkce_pair_matches_rfc7636_vector():
    # RFC 7636 Appendix B test vector.
    import hashlib

    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    assert expected == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    verifier2, challenge2 = generate_pkce_pair()
    assert len(verifier2) >= 43
    assert challenge2 == base64.urlsafe_b64encode(
        hashlib.sha256(verifier2.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")


def test_build_authorize_url_params():
    url, challenge = build_authorize_url("client-1", "verifier-1")

    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    query = dict(parse_qsl(urlparse(url).query))
    assert query["client_id"] == "client-1"
    assert query["response_type"] == "code"
    assert query["redirect_uri"] == "https://login.microsoftonline.com/common/oauth2/nativeclient"
    assert query["code_challenge"] == challenge
    assert query["code_challenge_method"] == "S256"
    assert "sydney" in query["scope"]


def _fake_jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{body}.signature"


def test_decode_jwt_payload():
    claims = decode_jwt_payload(_fake_jwt({"oid": "o", "tid": "t"}))

    assert claims["oid"] == "o"
    assert claims["tid"] == "t"


def _point_at_local_server(monkeypatch, port: int) -> None:
    """Redirect stream_chat_turn at a local test server instead of Microsoft."""
    monkeypatch.setattr(
        sydney, "build_ws_url", lambda **kwargs: f"ws://127.0.0.1:{port}/test"
    )


def test_stream_chat_turn_against_fake_server(monkeypatch):
    """Speak the real SignalR framing against a scripted WebSocket server."""
    import websockets.sync.server

    script = [
        ("recv_handshake", None),
        ("send", "{}\x1e"),
        ("recv_chat", None),
        (
            "send",
            json.dumps(
                {
                    "type": 1,
                    "target": "update",
                    "arguments": [{"messages": [{"author": "bot", "text": "Hello"}]}],
                }
            )
            + "\x1e",
        ),
        (
            "send",
            json.dumps(
                {
                    "type": 1,
                    "target": "update",
                    "arguments": [{"messages": [{"author": "bot", "text": "Hello world"}]}],
                }
            )
            + "\x1e",
        ),
        ("send", json.dumps({"type": 2, "item": {"messages": []}}) + "\x1e"),
    ]
    seen: list[str] = []

    def handler(ws):
        for action, payload in script:
            if action == "recv_handshake":
                data = ws.recv()
                assert json.loads(data.rstrip("\x1e")) == {"protocol": "json", "version": 1}
            elif action == "recv_chat":
                data = ws.recv()
                seen.append(data)
                assert '"target": "chat"' in data
                assert '"target": "Metrics"' in data  # same send, 0x1E-separated
            elif action == "send":
                ws.send(payload)

    server = websockets.sync.server.serve(handler, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    _point_at_local_server(monkeypatch, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario():
        conversation = SydneyConversation()
        deltas = []
        async for delta in sydney.stream_chat_turn(
            token="tok",
            claims={"oid": "o", "tid": "t"},
            conversation=conversation,
            text="hi",
            tone="magic",
        ):
            deltas.append(delta)
        return deltas, conversation

    try:
        deltas, conversation = asyncio.run(scenario())
    finally:
        server.shutdown()

    assert deltas == ["Hello", " world"]
    assert conversation.turn_count == 1
    # The chat invocation and the Metrics frame went out in one send.
    assert seen and seen[0].count("\x1e") >= 2


def test_stream_chat_turn_surfaces_server_error(monkeypatch):
    import websockets.sync.server

    def handler(ws):
        ws.recv()  # handshake
        ws.send("{}\x1e")
        ws.recv()  # chat + metrics
        ws.send(json.dumps({"type": 3, "error": "boom"}) + "\x1e")

    server = websockets.sync.server.serve(handler, "127.0.0.1", 0)
    _point_at_local_server(monkeypatch, server.socket.getsockname()[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario():
        async for _ in sydney.stream_chat_turn(
            token="tok",
            claims={"oid": "o", "tid": "t"},
            conversation=SydneyConversation(),
            text="hi",
            tone="magic",
        ):
            pass

    try:
        with pytest.raises(sydney.SydneyError, match="boom"):
            asyncio.run(scenario())
    finally:
        server.shutdown()


def test_stream_chat_turn_disengaged_raises(monkeypatch):
    import websockets.sync.server

    def handler(ws):
        ws.recv()
        ws.send("{}\x1e")
        ws.recv()
        ws.send(
            json.dumps(
                {
                    "type": 1,
                    "target": "update",
                    "arguments": [
                        {"messages": [{"author": "bot", "text": "", "messageType": "Disengaged"}]}
                    ],
                }
            )
            + "\x1e"
        )
        ws.send(json.dumps({"type": 2, "item": {"messages": []}}) + "\x1e")

    server = websockets.sync.server.serve(handler, "127.0.0.1", 0)
    _point_at_local_server(monkeypatch, server.socket.getsockname()[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario():
        async for _ in sydney.stream_chat_turn(
            token="tok",
            claims={"oid": "o", "tid": "t"},
            conversation=SydneyConversation(),
            text="hi",
            tone="magic",
        ):
            pass

    try:
        with pytest.raises(sydney.DisengagedError):
            asyncio.run(scenario())
    finally:
        server.shutdown()
