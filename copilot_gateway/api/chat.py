import hashlib
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from copilot_gateway.config import Settings
from copilot_gateway.models.request import ChatCompletionRequest
from copilot_gateway.models.response import (
    AssistantMessage,
    ChatCompletion,
    ChatCompletionChunk,
    ChunkChoice,
    ChunkDelta,
    ChunkFunction,
    ChunkToolCall,
    Choice,
    ModelInfo,
    ModelList,
    ToolCallFunctionOut,
    ToolCallOut,
    Usage,
)
from copilot_gateway.provider.base import ChatProvider, TextDelta, ToolCallsEvent
from copilot_gateway.provider.errors import BadRequest, GatewayError
from copilot_gateway.provider.prompt import NormalizedMessage, normalize_messages
from copilot_gateway.provider.sydney import AVAILABLE_MODELS
from copilot_gateway.utils.logging import get_logger

router = APIRouter()
log = get_logger("api")


def get_provider(request: Request) -> ChatProvider:
    return request.app.state.provider


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/v1/models")
@router.get("/models")
async def list_models() -> ModelList:
    return ModelList(data=[ModelInfo(id=model_id) for model_id in AVAILABLE_MODELS])


@router.get("/v1/models/{model_id}")
@router.get("/models/{model_id}")
async def get_model(model_id: str) -> ModelInfo:
    if model_id not in AVAILABLE_MODELS:
        raise BadRequest(f"Unknown model {model_id!r}")
    return ModelInfo(id=model_id)


@router.post("/v1/chat/completions", response_model=ChatCompletion, response_model_exclude_none=True)
@router.post("/chat/completions", response_model=ChatCompletion, response_model_exclude_none=True)
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    provider: ChatProvider = Depends(get_provider),
    settings: Settings = Depends(get_settings),
    x_session_id: str | None = Header(default=None),
) -> ChatCompletion | StreamingResponse:
    request_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()

    messages = normalize_messages(body.messages, settings.max_file_bytes)
    if not any(m.role in ("user", "tool") for m in messages):
        raise BadRequest("At least one user message is required")
    session_key = resolve_session_key(x_session_id or body.user, messages)
    prompt_chars = sum(len(m.text) for m in messages)
    # Header names only, never values, so credentials are not logged.
    log.info(
        "request=%s header_names=%s has_user_field=%s",
        request_id,
        sorted(request.headers.keys()),
        body.user is not None,
    )

    log.info(
        "request=%s session=%s model=%s prompt_chars=%d files=%d images=%d tools=%d stream=%s",
        request_id,
        session_key,
        body.model,
        prompt_chars,
        sum(len(m.files) for m in messages),
        sum(len(m.images) for m in messages),
        len(body.tools or []),
        body.stream,
    )
    include_usage = bool((body.stream_options or {}).get("include_usage"))

    try:
        if body.stream:
            return StreamingResponse(
                _sse(request_id, body, provider, session_key, messages, include_usage),
                media_type="text/event-stream",
            )
        completion = await _collect(request_id, body, provider, session_key, messages)
    except GatewayError as exc:
        # Expected failures (bad request, auth, provider errors) log briefly;
        # unexpected ones keep the traceback.
        if exc.status_code >= 500:
            log.error(
                "request=%s failed after %.0f ms",
                request_id,
                (time.perf_counter() - started) * 1000,
                exc_info=True,
            )
        else:
            log.warning("request=%s failed: %s", request_id, exc)
        raise
    except Exception:
        log.error(
            "request=%s failed after %.0f ms",
            request_id,
            (time.perf_counter() - started) * 1000,
            exc_info=True,
        )
        raise

    latency_ms = (time.perf_counter() - started) * 1000
    log.info(
        "request=%s finish=%s response_chars=%d latency_ms=%.0f",
        request_id,
        completion.choices[0].finish_reason,
        len(completion.choices[0].message.content or ""),
        latency_ms,
    )
    return completion


def resolve_session_key(explicit: str | None, messages: list[NormalizedMessage]) -> str:
    """Pick the key that identifies one coding-agent conversation.

    Clients that send X-Session-Id or the OpenAI `user` field get exact isolation.
    Otherwise the first client (user/tool) message is fingerprinted. That is stable
    across turns of one conversation, but two conversations that start with the same
    message share a thread.
    """
    if explicit:
        return f"explicit:{explicit}"
    anchor = next(m for m in messages if m.role in ("user", "tool"))
    digest = hashlib.sha256(f"{anchor.role}\n{anchor.text}".encode("utf-8")).hexdigest()
    return f"auto:{digest[:16]}"


async def _collect(
    request_id: str,
    body: ChatCompletionRequest,
    provider: ChatProvider,
    session_key: str,
    messages: list[NormalizedMessage],
) -> ChatCompletion:
    text_parts: list[str] = []
    tool_calls: list[ToolCallOut] = []
    async for event in provider.run(
        session_key,
        messages,
        tools=body.tools,
        tool_choice=body.tool_choice,
        model=body.model,
    ):
        if isinstance(event, TextDelta):
            text_parts.append(event.text)
        elif isinstance(event, ToolCallsEvent):
            tool_calls.extend(
                ToolCallOut(
                    id=call.id,
                    function=ToolCallFunctionOut(name=call.name, arguments=call.arguments),
                )
                for call in event.calls
            )
    text = "".join(text_parts)
    completion_id = f"chatcmpl-{request_id}"
    created = int(time.time())
    if tool_calls:
        message = AssistantMessage(
            content=text.strip() or None,
            tool_calls=tool_calls,
        )
        finish_reason = "tool_calls"
    else:
        message = AssistantMessage(content=text)
        finish_reason = "stop"
    prompt_chars = sum(len(m.text) for m in messages)
    return ChatCompletion(
        id=completion_id,
        created=created,
        model=body.model,
        choices=[Choice(message=message, finish_reason=finish_reason)],
        usage=_estimate_usage(prompt_chars, len(text)),
    )


async def _sse(
    request_id: str,
    body: ChatCompletionRequest,
    provider: ChatProvider,
    session_key: str,
    messages: list[NormalizedMessage],
    include_usage: bool,
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl-{request_id}"
    created = int(time.time())
    prompt_chars = sum(len(m.text) for m in messages)

    def chunk(
        delta: ChunkDelta | None = None,
        finish_reason: str | None = None,
        usage: Usage | None = None,
    ) -> str:
        payload = ChatCompletionChunk(
            id=completion_id,
            created=created,
            model=body.model,
            choices=[ChunkChoice(delta=delta or ChunkDelta(), finish_reason=finish_reason)],
            usage=usage,
        )
        return f"data: {payload.model_dump_json(exclude_none=True)}\n\n"

    try:
        # The role-only first chunk, as OpenAI sends it.
        yield chunk(ChunkDelta(role="assistant"))
        text_chars = 0
        finish_reason = "stop"
        async for event in provider.run(
            session_key,
            messages,
            tools=body.tools,
            tool_choice=body.tool_choice,
            model=body.model,
        ):
            if isinstance(event, TextDelta):
                text_chars += len(event.text)
                yield chunk(ChunkDelta(content=event.text))
            elif isinstance(event, ToolCallsEvent):
                for index, call in enumerate(event.calls):
                    yield chunk(
                        ChunkDelta(
                            tool_calls=[
                                ChunkToolCall(
                                    index=index,
                                    id=call.id,
                                    type="function",
                                    function=ChunkFunction(
                                        name=call.name, arguments=call.arguments
                                    ),
                                )
                            ]
                        )
                    )
                finish_reason = "tool_calls"
        usage = _estimate_usage(prompt_chars, text_chars) if include_usage else None
        yield chunk(finish_reason=finish_reason, usage=usage)
        yield "data: [DONE]\n\n"
    except Exception as exc:
        # The stream dies here; the error is already logged by the caller path.
        log.error("request=%s stream failed: %s", request_id, exc)
        raise


def _estimate_usage(prompt_chars: int, completion_chars: int) -> Usage:
    prompt_tokens = max(1, prompt_chars // 4)
    completion_tokens = max(1, completion_chars // 4)
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
