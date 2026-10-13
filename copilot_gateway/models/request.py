from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


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


# Any other part type (image_url, input_audio, ...) fails validation -> 422.
ContentPart = Annotated[Union[TextPart, FilePart], Field(discriminator="type")]


class ChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[ContentPart] | None = None


class ChatCompletionRequest(BaseModel):
    model: str = "copilot-web"
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    user: str | None = None
    # Other OpenAI fields (temperature, max_tokens, tools, ...) are accepted and ignored.
