from typing import Literal

from pydantic import BaseModel


class Usage(BaseModel):
    # Estimates only (about 4 characters per token); Copilot reports no usage.
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list["ToolCallOut"] | None = None


class ToolCallFunctionOut(BaseModel):
    name: str
    arguments: str


class ToolCallOut(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunctionOut


class Choice(BaseModel):
    index: int = 0
    message: AssistantMessage
    finish_reason: str = "stop"


class ChatCompletion(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage


class ChunkFunction(BaseModel):
    name: str | None = None
    arguments: str | None = None


class ChunkToolCall(BaseModel):
    index: int = 0
    id: str | None = None
    type: Literal["function"] | None = None
    function: ChunkFunction | None = None


class ChunkDelta(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None
    tool_calls: list[ChunkToolCall] | None = None


class ChunkChoice(BaseModel):
    index: int = 0
    delta: ChunkDelta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice]
    usage: Usage | None = None


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "copilot-gateway"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]
