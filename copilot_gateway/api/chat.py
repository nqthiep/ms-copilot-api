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
    Choice,
    ModelInfo,
    ModelList,
    Usage,
)
from copilot_gateway.provider.base import ChatProvider
from copilot_gateway.provider.errors import BadRequest
from copilot_gateway.provider.prompt import NormalizedMessage, normalize_messages
from copilot_gateway.utils.logging import get_logger

router = APIRouter()
log = get_logger("api")

MODEL_ID = "copilot-web"


def get_provider(request: Request) -> ChatProvider:
    return request.app.state.provider


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/v1/models")
async def list_models() -> ModelList:
    return ModelList(data=[ModelInfo(id=MODEL_ID)])


@router.post("/v1/chat/completions", response_model=None)
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
    if not any(m.role == "user" for m in messages):
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
        "request=%s session=%s prompt_chars=%d files=%d stream=%s",
        request_id,
        session_key,
        prompt_chars,
        sum(len(m.files) for m in messages),
        body.stream,
    )
    try:
        reply = await provider.complete(session_key, messages)
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
        "request=%s response_chars=%d latency_ms=%.0f",
        request_id,
        len(reply),
        latency_ms,
    )

    completion_id = f"chatcmpl-{request_id}"
    created = int(time.time())
    if body.stream:
        return StreamingResponse(
            _sse(completion_id, created, body.model, reply),
            media_type="text/event-stream",
        )
    return ChatCompletion(
        id=completion_id,
        created=created,
        model=body.model,
        choices=[Choice(message=AssistantMessage(content=reply))],
        usage=_estimate_usage(prompt_chars, len(reply)),
    )


def resolve_session_key(explicit: str | None, messages: list[NormalizedMessage]) -> str:
    """Pick the key that identifies one coding-agent conversation.

    Clients that send X-Session-Id or the OpenAI `user` field get exact isolation.
    Otherwise the first non-system message is fingerprinted. That is stable across
    turns of one conversation, but two conversations that start with the same
    message share a thread.
    """
    if explicit:
        return f"explicit:{explicit}"
    anchor = next(m for m in messages if m.role != "system")
    digest = hashlib.sha256(f"{anchor.role}\n{anchor.text}".encode("utf-8")).hexdigest()
    return f"auto:{digest[:16]}"


async def _sse(completion_id: str, created: int, model: str, reply: str) -> AsyncIterator[str]:
    # The reply is produced in full before streaming starts, so it arrives as one content chunk.
    first = ChatCompletionChunk(
        id=completion_id,
        created=created,
        model=model,
        choices=[ChunkChoice(delta=ChunkDelta(role="assistant", content=reply))],
    )
    last = ChatCompletionChunk(
        id=completion_id,
        created=created,
        model=model,
        choices=[ChunkChoice(delta=ChunkDelta(), finish_reason="stop")],
    )
    yield f"data: {first.model_dump_json()}\n\n"
    yield f"data: {last.model_dump_json()}\n\n"
    yield "data: [DONE]\n\n"


def _estimate_usage(prompt_chars: int, completion_chars: int) -> Usage:
    prompt_tokens = max(1, prompt_chars // 4)
    completion_tokens = max(1, completion_chars // 4)
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
