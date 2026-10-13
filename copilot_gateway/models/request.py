from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class FileRef(BaseModel):
    filename: str
    # OpenAI-style: a data URL ("data:<mime>;base64,...") or raw base64.
    file_data: str


class TextPart(BaseModel):
    type: Literal["text"]
    text: str


class FilePart(BaseModel):
    type: Literal["file"]
    file: FileRef


class ImageUrl(BaseModel):
    # A data URL ("data:image/png;base64,..."). Remote http(s) URLs are rejected.
    url: str
    detail: str | None = None


class ImagePart(BaseModel):
    type: Literal["image_url"]
    image_url: ImageUrl


# Any other part type (input_audio, ...) fails validation -> 422.
ContentPart = Annotated[Union[TextPart, FilePart, ImagePart], Field(discriminator="type")]


class FunctionDef(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class ToolDef(BaseModel):
    type: Literal["function"] = "function"
    function: FunctionDef


class ToolCallFunction(BaseModel):
    name: str | None = None
    arguments: str | None = None


class ToolCallRef(BaseModel):
    id: str | None = None
    type: Literal["function"] | None = None
    function: ToolCallFunction | None = None


class ChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    # Set on assistant messages when the client echoes back previous tool calls.
    tool_calls: list[ToolCallRef] | None = None
    # Set on tool messages: which call this result answers.
    tool_call_id: str | None = None
    # Function name for tool messages (OpenAI convention).
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = "m365-copilot"
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    user: str | None = None
    tools: list[ToolDef] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    # Accepted and mostly ignored: the Sydney backend picks sampling itself.
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stop: str | list[str] | None = None
    n: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    logit_bias: dict[str, float] | None = None
    seed: int | None = None
    stream_options: dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
