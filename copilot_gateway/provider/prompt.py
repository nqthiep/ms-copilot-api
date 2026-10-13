import base64
import binascii
import os
from dataclasses import dataclass

from copilot_gateway.models.request import ChatMessage
from copilot_gateway.provider.errors import BadRequest

ALLOWED_EXTENSIONS = {".py", ".md", ".txt", ".docx", ".pdf", ".json", ".yaml", ".yml"}
# Extensions whose content is inlined into the prompt as text.
TEXT_EXTENSIONS = {".py", ".md", ".txt", ".json", ".yaml", ".yml"}
ROLE_ALIASES = {"developer": "system"}


@dataclass(frozen=True)
class AttachedFile:
    filename: str
    data: bytes


@dataclass(frozen=True)
class AttachedImage:
    filename: str
    data: bytes
    mime: str


@dataclass(frozen=True)
class NormalizedToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, as the client sent it


@dataclass(frozen=True)
class NormalizedMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    text: str
    files: tuple[AttachedFile, ...] = ()
    images: tuple[AttachedImage, ...] = ()
    tool_calls: tuple[NormalizedToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None


def normalize_messages(messages: list[ChatMessage], max_file_bytes: int) -> list[NormalizedMessage]:
    return [_normalize(message, max_file_bytes) for message in messages]


def _normalize(message: ChatMessage, max_file_bytes: int) -> NormalizedMessage:
    role = ROLE_ALIASES.get(message.role, message.role)
    tool_calls = tuple(
        NormalizedToolCall(
            id=tc.id or "",
            name=(tc.function.name if tc.function else None) or "",
            arguments=(tc.function.arguments if tc.function else None) or "{}",
        )
        for tc in (message.tool_calls or [])
    )
    content = message.content
    if content is None:
        return NormalizedMessage(
            role=role,
            text="",
            tool_calls=tool_calls,
            tool_call_id=message.tool_call_id,
            tool_name=message.name,
        )
    if isinstance(content, str):
        return NormalizedMessage(
            role=role,
            text=content,
            tool_calls=tool_calls,
            tool_call_id=message.tool_call_id,
            tool_name=message.name,
        )

    texts: list[str] = []
    files: list[AttachedFile] = []
    images: list[AttachedImage] = []
    for part in content:
        if part.type == "text":
            texts.append(part.text)
        elif part.type == "image_url":
            images.append(_decode_image(part.image_url.url, max_file_bytes))
        else:
            files.append(_decode_file(part.file.filename, part.file.file_data, max_file_bytes))
    return NormalizedMessage(
        role=role,
        text="\n".join(texts),
        files=tuple(files),
        images=tuple(images),
        tool_calls=tool_calls,
        tool_call_id=message.tool_call_id,
        tool_name=message.name,
    )


def _decode_file(filename: str, file_data: str, max_file_bytes: int) -> AttachedFile:
    name = os.path.basename(filename) or "attachment"
    if os.path.splitext(name)[1].lower() not in ALLOWED_EXTENSIONS:
        raise BadRequest(f"Unsupported file type for {name!r}")

    payload = file_data.split(",", 1)[1] if file_data.startswith("data:") else file_data
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BadRequest(f"file_data for {name!r} is not valid base64") from exc
    if len(data) > max_file_bytes:
        raise BadRequest(f"{name!r} exceeds the {max_file_bytes} byte limit")
    return AttachedFile(filename=name, data=data)


def _decode_image(url: str, max_file_bytes: int) -> AttachedImage:
    if not url.startswith("data:"):
        raise BadRequest(
            "Remote image URLs are not supported; send the image as a data: URL instead"
        )
    header, _, payload = url.partition(",")
    mime = header[5:].split(";")[0] or "image/png"
    if not mime.startswith("image/"):
        raise BadRequest(f"Unsupported image MIME type {mime!r}")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BadRequest("image_url data is not valid base64") from exc
    if len(data) > max_file_bytes:
        raise BadRequest(f"Image exceeds the {max_file_bytes} byte limit")
    ext = mime.split("/", 1)[1].split("+")[0]
    return AttachedImage(filename=f"image.{ext}", data=data, mime=mime)


def compile_prompt(messages: list[NormalizedMessage]) -> str:
    """Full conversation as one prompt, used for a fresh Copilot thread."""
    system = "\n\n".join(m.text for m in messages if m.role == "system" and m.text)
    turns = [m for m in messages if m.role != "system"]

    parts: list[str] = []
    if system:
        parts.append(f"SYSTEM\n\n{system}")
    parts.append("CONVERSATION\n\n" + "\n\n".join(_format_turn(m) for m in turns))
    parts.append("Reply only to the last USER message.")
    return "\n\n".join(parts)


def format_user_turns(messages: list[NormalizedMessage]) -> str:
    """New user messages for a Copilot thread that already holds the earlier context."""
    return "\n\n".join(_format_turn(m) for m in messages)


def _format_turn(message: NormalizedMessage) -> str:
    label = "USER" if message.role == "user" else "ASSISTANT"
    return f"{label}:\n{message.text}"


def pending_user_turns(messages: list[NormalizedMessage], sent: int) -> list[NormalizedMessage]:
    """The user messages the Copilot thread has not received yet.

    `sent` is how many user turns the thread already holds. A client that resends the
    whole history gets only its new turns. A client that sends just the newest message
    gets that message, because at least one turn is always sent.
    """
    users = [m for m in messages if m.role == "user"]
    count = max(len(users) - sent, 1)
    return users[-count:]


def pending_client_turns(messages: list[NormalizedMessage], sent: int) -> list[NormalizedMessage]:
    """The client turns (user + tool messages) the backend conversation has not seen yet.

    Same contract as pending_user_turns, but tool results count as turns too, so an
    agent loop that only appends tool results still makes progress.
    """
    turns = [m for m in messages if m.role in ("user", "tool")]
    count = max(len(turns) - sent, 1)
    return turns[-count:]
