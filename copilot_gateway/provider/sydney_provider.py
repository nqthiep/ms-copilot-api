"""Chat provider that talks to M365 Copilot over its SignalR/WebSocket API.

One gateway session maps to one server-side Sydney conversation (the ids are
persisted in the session store), so multi-turn agent loops keep context. Only
the turns the conversation has not seen are sent; each turn opens a fresh
WebSocket.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator

import httpx

from copilot_gateway.config import Settings
from copilot_gateway.models.request import ToolDef
from copilot_gateway.provider.base import (
    ChatProvider,
    ProviderEvent,
    TextDelta,
    ToolCall,
    ToolCallsEvent,
)
from copilot_gateway.provider.errors import AuthRequired
from copilot_gateway.provider.m365_auth import TokenManager, TokenSet, decode_jwt_payload
from copilot_gateway.provider.prompt import NormalizedMessage, pending_client_turns
from copilot_gateway.provider.sydney import (
    EmptyResponse,
    SydneyConversation,
    SydneyError,
    UploadedImage,
    stream_chat_turn,
    tone_for_model,
    upload_image,
)
from copilot_gateway.provider.tools import (
    ParsedToolCall,
    compile_sydney_turn,
    parse_tool_calls,
    strip_tool_calls_block,
    tools_active,
)
from copilot_gateway.storage.store import SessionStore, ThreadState
from copilot_gateway.utils.logging import get_logger

log = get_logger("sydney-provider")

# Failures worth another attempt on a fresh conversation.
RETRIABLE = (EmptyResponse, SydneyError)


def client_turn_count(messages: list[NormalizedMessage]) -> int:
    return sum(1 for m in messages if m.role in ("user", "tool"))


def starts_new_chat(session_key: str, messages: list[NormalizedMessage], state: ThreadState) -> bool:
    """True when this request cannot be a continuation of the stored conversation.

    - The client holds fewer turns than the conversation: it began a new chat.
    - An auto key (no client identifier) and the client did not echo the assistant's
      earlier reply: a new chat that reuses the same first message. A client that
      continues a conversation always resends the reply it received.

    Requests with an explicit X-Session-Id are trusted, so a client that sends only
    its newest message keeps one conversation.
    """
    if client_turn_count(messages) < state.sydney_turns:
        return True
    echoes_reply = any(m.role == "assistant" for m in messages)
    return session_key.startswith("auto:") and state.sydney_turns > 0 and not echoes_reply


class SydneyProvider(ChatProvider):
    """Runs each chat turn on the Sydney ChatHub, one request at a time."""

    def __init__(
        self,
        settings: Settings,
        store: SessionStore | None = None,
        tokens: TokenManager | None = None,
    ) -> None:
        self._settings = settings
        self._store = store or SessionStore(settings.sessions_path)
        self._tokens = tokens or TokenManager(settings)
        self._lock = asyncio.Lock()
        # Built lazily: httpx reads ambient proxy env vars at construction, and
        # some environments set values its URL parser chokes on. trust_env=False
        # keeps behavior deterministic; the configured proxy is applied instead.
        self._http: httpx.AsyncClient | None = None
        self._auth_ready = False

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            kwargs: dict = {"timeout": 30, "trust_env": False}
            proxy = _proxy_url(self._settings)
            if proxy:
                kwargs["proxy"] = proxy
            self._http = httpx.AsyncClient(**kwargs)
        return self._http

    async def start(self) -> None:
        try:
            await self._tokens.get_token(interactive=False)
            self._auth_ready = True
            log.info("Microsoft token is ready")
        except AuthRequired:
            self._auth_ready = False
            log.info("No Microsoft token; opening the sign-in window in the background")
            self._tokens.start_interactive_login()

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def health(self) -> dict[str, object]:
        return {"provider": "sydney", "auth_ready": self._auth_ready}

    async def complete(self, session_key: str, messages: list[NormalizedMessage]) -> str:
        parts: list[str] = []
        async for event in self.run(session_key, messages):
            if isinstance(event, TextDelta):
                parts.append(event.text)
        return "".join(parts)

    async def run(
        self,
        session_key: str,
        messages: list[NormalizedMessage],
        *,
        tools: list[ToolDef] | None = None,
        tool_choice: object = None,
        model: str = "m365-copilot",
    ) -> AsyncIterator[ProviderEvent]:
        await self._lock.acquire()
        try:
            async for event in self._run_locked(session_key, messages, tools, tool_choice, model):
                yield event
        finally:
            self._lock.release()

    async def _run_locked(
        self,
        session_key: str,
        messages: list[NormalizedMessage],
        tools: list[ToolDef] | None,
        tool_choice: object,
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        deadline = time.monotonic() + self._settings.request_timeout
        token = await self._ensure_token()
        state = self._store.get(session_key)
        if state is not None and starts_new_chat(session_key, messages, state):
            log.info(
                "Session %s looks like a new chat (%d client turns, conversation holds %d); "
                "starting a new Sydney conversation",
                session_key,
                client_turn_count(messages),
                state.sydney_turns,
            )
            state = None
        conversation = SydneyConversation(
            session_id=(state.sydney_session_id if state else None) or "",
            conversation_id=(state.sydney_conversation_id if state else None) or "",
            turn_count=state.sydney_turns if state else 0,
            resumed=bool(state and state.sydney_conversation_id),
        )
        sent = state.sydney_turns if state else 0
        use_tools = tools_active(tools, tool_choice)

        attempts = self._settings.max_retry + 1
        for attempt in range(1, attempts + 1):
            fresh = attempt > 1
            if fresh:
                log.warning("Retrying on a fresh Sydney conversation (attempt %d/%d)", attempt, attempts)
                self._store.forget(session_key)
                conversation = SydneyConversation()
                sent = 0
            pending = pending_client_turns(messages, sent)
            text = compile_sydney_turn(
                messages,
                pending,
                tools=tools,
                tool_choice=tool_choice,
                is_first=conversation.turn_count == 0 and not conversation.resumed,
                max_inline_file_chars=self._settings.max_inline_file_chars,
            )
            tone = tone_for_model(model, self._settings.m365_default_tone)
            remaining = max(30.0, deadline - time.monotonic())

            try:
                uploads = await self._upload_images(token, conversation, pending)
                deltas: list[str] = []
                # With tools active the reply is buffered: the model may emit a
                # ```tool_calls fence that must be parsed (and hidden) before
                # anything is yielded. Otherwise deltas stream live.
                async for delta in stream_chat_turn(
                    token=token,
                    claims=decode_jwt_payload(token),
                    conversation=conversation,
                    text=text,
                    tone=tone,
                    images=uploads,
                    timeout=remaining,
                    code_interpreter=not self._settings.m365_no_code_interpreter,
                    proxy=_proxy_url(self._settings),
                ):
                    deltas.append(delta)
                    if not use_tools:
                        yield TextDelta(delta)
            except AuthRequired:
                # The token died mid-run (revoked/expired). Drop the cached
                # access token and try once more with a fresh one.
                if attempt == attempts:
                    raise
                log.warning("Token rejected mid-turn; refreshing and retrying once")
                token = await self._refresh_after_rejection()
                continue
            except RETRIABLE as exc:
                if attempt == attempts:
                    raise
                log.warning("Attempt %d/%d failed (%s); retrying", attempt, attempts, exc)
                continue

            reply = "".join(deltas)
            calls = self._extract_tool_calls(reply, tools) if use_tools else None
            if use_tools and not calls and '"arguments"' in reply:
                # The model tried to call a tool but the block did not parse. Say so in
                # the log; the reply itself is still returned as text.
                log.warning("Reply looks like a tool call but no valid call parsed; returned as text")
            if use_tools:
                # Hide the fence; keep any surrounding prose as the content.
                text_out = strip_tool_calls_block(reply, tools or []) if calls else reply
                if text_out:
                    yield TextDelta(text_out)
                if calls:
                    yield ToolCallsEvent(calls=tuple(calls))
            if not reply.strip() and not calls:
                if attempt == attempts:
                    raise EmptyResponse("Copilot returned an empty response")
                log.warning("Attempt %d/%d returned empty; retrying", attempt, attempts)
                continue

            self._store.put(
                session_key,
                ThreadState(
                    sydney_session_id=conversation.session_id,
                    sydney_conversation_id=conversation.conversation_id,
                    sydney_turns=client_turn_count(messages),
                ),
            )
            self._auth_ready = True
            return

        raise AssertionError("unreachable")

    async def _ensure_token(self) -> str:
        try:
            return await self._tokens.get_token(interactive=True)
        except AuthRequired:
            self._auth_ready = False
            raise

    async def _refresh_after_rejection(self) -> str:
        # Bypass the cached access token; the refresh grant mints a new one.
        from copilot_gateway.provider.m365_auth import TokenStore

        store = TokenStore(self._settings.token_path)
        saved = store.load()
        if saved is not None:
            store.save(TokenSet(access_token="", refresh_token=saved.refresh_token, expires_at=0))
        return await self._tokens.get_token(interactive=True)

    async def _upload_images(
        self, token: str, conversation: SydneyConversation, pending: list[NormalizedMessage]
    ) -> list[UploadedImage]:
        uploads: list[UploadedImage] = []
        for message in pending:
            for image in message.images:
                uploads.append(
                    await upload_image(
                        self._http_client(),
                        token=token,
                        conversation_id=conversation.conversation_id,
                        filename=image.filename,
                        data=image.data,
                        mime=image.mime,
                    )
                )
        return uploads

    def _extract_tool_calls(
        self, reply: str, tools: list[ToolDef] | None
    ) -> list[ToolCall] | None:
        parsed: list[ParsedToolCall] | None = parse_tool_calls(reply, tools or [])
        if not parsed:
            return None
        return [
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:12]}",
                name=call.name,
                arguments=json.dumps(call.arguments, ensure_ascii=False),
            )
            for call in parsed
        ]


def _proxy_url(settings: Settings) -> str | None:
    """Build a proxy URL from settings, with credentials when configured."""
    if not settings.proxy_server:
        return None
    server = settings.proxy_server
    if settings.proxy_username:
        password = settings.proxy_password or ""
        scheme, _, rest = server.partition("://")
        auth = f"{settings.proxy_username}:{password}@"
        server = f"{scheme}://{auth}{rest}" if rest else f"{scheme}://{auth}"
    return server
