"""Tool-calling emulation for the Sydney backend.

M365 Copilot has no native function-calling API, so when a client (such as the
VSCode Copilot Chat extension in agent mode) sends ``tools``, the gateway:

1. describes the tools in a system-style preamble and instructs the model to
   emit calls as a single fenced `````tool_calls`` JSON block,
2. parses that block out of the reply and returns the calls in OpenAI's
   ``tool_calls`` shape,
3. feeds the client's ``role: tool`` results back as a follow-up turn.

This is a shim, not native tool use: the model may occasionally misformat a
call, in which case the gateway falls back to treating the reply as plain
text.
"""

import json
import re
from dataclasses import dataclass

from copilot_gateway.models.request import ToolDef
from copilot_gateway.provider.errors import BadRequest
from copilot_gateway.provider.prompt import TEXT_EXTENSIONS, AttachedFile, NormalizedMessage

# Models drift from the requested format: a ```json or ```tool_call fence, a bare
# object instead of an array, or a call nested under "function". All of these are
# accepted. A block only counts when its calls name known tools.
_FENCE_RE = re.compile(r"```(tool_calls?|json)[ \t]*\n?(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class ParsedToolCall:
    name: str
    arguments: dict


def build_tools_preamble(tools: list[ToolDef], tool_choice: object) -> str:
    """Describe the tools and the exact output format for calling them."""
    lines = [
        "You have access to the following functions. To call one or more of them, "
        "output a single fenced block exactly like this:",
        "",
        "```tool_calls",
        '[{"name": "function_name", "arguments": {"arg": "value"}}]',
        "```",
        "",
        "Available functions:",
    ]
    for tool in tools:
        params = tool.function.parameters or {"type": "object", "properties": {}}
        lines.append(
            f"- {tool.function.name}: {(tool.function.description or 'No description.').strip()} "
            f"Parameters JSON schema: {json.dumps(params, ensure_ascii=False)}"
        )
    lines += [
        "Rules:",
        "- Output at most one ```tool_calls block per response; it holds a JSON array "
        "(it may contain several calls, which run in order).",
        '- Each "arguments" value must be a JSON object matching the function parameters.',
        "- Put any explanation outside the fenced block, never inside it.",
        "- When no function is needed, answer normally without the block.",
    ]
    choice = _normalize_choice(tool_choice)
    if choice == "required":
        lines.append("You MUST call at least one function in this response.")
    elif choice.startswith("function:"):
        lines.append(
            f"You MUST call the function `{choice[len('function:'):].strip()}` in this response."
        )
    return "\n".join(lines)


def _normalize_choice(tool_choice: object) -> str:
    if tool_choice is None:
        return "auto"
    if isinstance(tool_choice, str):
        return tool_choice.lower()
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        name = fn.get("name") if isinstance(fn, dict) else None
        if tool_choice.get("type") == "function" and name:
            return f"function:{name}"
        return str(tool_choice.get("type", "auto")).lower()
    return "auto"


def tools_active(tools: list[ToolDef] | None, tool_choice: object) -> bool:
    """Whether the turn should offer tool calling at all."""
    if not tools:
        return False
    return _normalize_choice(tool_choice) != "none"


def _parse_block(body: str, known: set[str]) -> list[ParsedToolCall] | None:
    """The calls in one fenced block, or None when the block is not a tool call."""
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return None
    items = raw if isinstance(raw, list) else [raw]
    calls: list[ParsedToolCall] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        inner = item["function"] if isinstance(item.get("function"), dict) else item
        name = inner.get("name")
        if isinstance(name, str):
            name = name.strip()
        arguments = inner.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                return None
        if name not in known or not isinstance(arguments, dict):
            return None
        calls.append(ParsedToolCall(name=name, arguments=arguments))
    return calls or None


def parse_tool_calls(text: str, tools: list[ToolDef]) -> list[ParsedToolCall] | None:
    """Extract tool calls from the fenced blocks in a reply.

    Blocks that are not tool calls (a JSON example, for instance) are ignored.
    Returns None when no block holds a valid call, so the caller can fall back to
    plain text.
    """
    known = {tool.function.name for tool in tools}
    calls: list[ParsedToolCall] = []
    for match in _FENCE_RE.finditer(text):
        calls.extend(_parse_block(match.group(2), known) or [])
    return calls or None


def strip_tool_calls_block(text: str, tools: list[ToolDef]) -> str:
    """Remove the tool-call blocks, keeping the surrounding prose.

    A block tagged tool_calls is always removed, even when malformed, so the user
    never sees raw tool syntax. A json block is removed only when it holds calls.
    """
    known = {tool.function.name for tool in tools}

    def drop_tool_block(match: re.Match[str]) -> str:
        tag, body = match.group(1).lower(), match.group(2)
        if tag != "json" or _parse_block(body, known):
            return ""
        return match.group(0)

    return _FENCE_RE.sub(drop_tool_block, text).strip()


def _inline_file(attachment: AttachedFile, max_chars: int) -> str:
    ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else ""
    if f".{ext}" not in TEXT_EXTENSIONS:
        raise BadRequest(
            f"Cannot inline {attachment.filename!r}: only text files "
            f"({sorted(TEXT_EXTENSIONS)}) are supported; convert it to text first"
        )
    content = attachment.data.decode("utf-8", errors="replace")
    if len(content) > max_chars:
        raise BadRequest(
            f"{attachment.filename!r} is {len(content)} characters; "
            f"the inline limit is {max_chars}. Split it into smaller pieces."
        )
    return f"\n\n[Attached file: {attachment.filename}]\n{content}"


def _render_message(message: NormalizedMessage, max_inline_file_chars: int) -> str:
    text = message.text
    for attachment in message.files:
        text += _inline_file(attachment, max_inline_file_chars)
    return text


def _format_history(message: NormalizedMessage, max_inline_file_chars: int) -> str:
    text = _render_message(message, max_inline_file_chars)
    if message.role == "user":
        return f"USER:\n{text}"
    if message.role == "tool":
        label = message.tool_name or "function"
        call_id = message.tool_call_id or "?"
        return f"TOOL RESULT [{call_id}] ({label}):\n{text}"
    # assistant
    if message.tool_calls:
        calls = "\n".join(
            f"- {tc.id or '?'}: {tc.name}({tc.arguments})" for tc in message.tool_calls
        )
        head = f"ASSISTANT (called tools):\n{calls}"
        return f"{head}\n{text}" if text else head
    return f"ASSISTANT:\n{text}"


def compile_sydney_turn(
    messages: list[NormalizedMessage],
    pending: list[NormalizedMessage],
    *,
    tools: list[ToolDef] | None,
    tool_choice: object,
    is_first: bool,
    max_inline_file_chars: int,
) -> str:
    """Compile one Sydney turn from the OpenAI-style conversation.

    The first turn carries the system prompt and the full history; later turns
    carry only what the backend has not seen yet (plus the assistant's tool
    calls for context when tool results follow).
    """
    blocks: list[str] = []
    if tools_active(tools, tool_choice):
        blocks.append(build_tools_preamble(tools or [], tool_choice))

    if is_first:
        system = "\n\n".join(m.text for m in messages if m.role == "system" and m.text)
        if system:
            blocks.append(f"SYSTEM\n\n{system}")
        history = [
            _format_history(m, max_inline_file_chars)
            for m in messages
            if m.role in ("user", "assistant", "tool") and (m.text or m.tool_calls)
        ]
        blocks.append("CONVERSATION\n\n" + "\n\n".join(history))
        blocks.append("Reply only to the last USER message.")
    else:
        context: list[str] = []
        # The assistant's tool calls give meaning to the tool results below.
        for message in reversed(messages):
            if message.role == "assistant" and message.tool_calls:
                context.append(_format_history(message, max_inline_file_chars))
                break
            if message.role == "user":
                break
        context.extend(
            _format_history(m, max_inline_file_chars)
            for m in pending
            if m.text or m.tool_calls
        )
        blocks.append("\n\n".join(context))
        if not any(m.role == "user" for m in pending):
            blocks.append(
                "Continue the task using the tool results above. "
                "Call more functions if needed, otherwise answer."
            )
    return "\n\n".join(block for block in blocks if block)
