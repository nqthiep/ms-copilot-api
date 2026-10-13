import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass

from copilot_gateway.models.request import ChatMessage
from copilot_gateway.provider.errors import BadRequest

ALLOWED_EXTENSIONS = {".py", ".md", ".txt", ".docx", ".pdf", ".json", ".yaml", ".yml"}
ROLE_ALIASES = {"developer": "system"}


@dataclass(frozen=True)
class AttachedFile:
    filename: str
    data: bytes


@dataclass(frozen=True)
class NormalizedMessage:
    role: str  # "system" | "user" | "assistant"
    text: str
    files: tuple[AttachedFile, ...] = ()


def normalize_messages(messages: list[ChatMessage], max_file_bytes: int) -> list[NormalizedMessage]:
    return [_normalize(message, max_file_bytes) for message in messages]


def _normalize(message: ChatMessage, max_file_bytes: int) -> NormalizedMessage:
    role = ROLE_ALIASES.get(message.role, message.role)
    content = message.content
    if content is None:
        return NormalizedMessage(role=role, text="")
    if isinstance(content, str):
        return NormalizedMessage(role=role, text=content)

    texts: list[str] = []
    files: list[AttachedFile] = []
    for part in content:
        if part.type == "text":
            texts.append(part.text)
        else:
            files.append(_decode_file(part.file.filename, part.file.file_data, max_file_bytes))
    return NormalizedMessage(role=role, text="\n".join(texts), files=tuple(files))


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


def prefix_hash(messages: list[NormalizedMessage]) -> str:
    canonical = [
        [m.role, m.text, [f.filename for f in m.files]] for m in messages
    ]
    return hashlib.sha256(json.dumps(canonical).encode("utf-8")).hexdigest()


def continuation_delta(
    messages: list[NormalizedMessage], stored_count: int, stored_hash: str
) -> list[NormalizedMessage] | None:
    """Return the new user messages if the thread still matches the stored prefix.

    Returns None when the thread cannot be reused: the client rewrote history,
    sent no new message, or the new messages are not plain user turns.
    """
    if stored_count <= 0 or len(messages) <= stored_count:
        return None
    if prefix_hash(messages[:stored_count]) != stored_hash:
        return None
    delta = messages[stored_count:]
    if any(m.role != "user" for m in delta):
        return None
    return delta
