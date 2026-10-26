"""Tests for the tool-calling emulation (prompt preamble + parsing)."""

import pytest

from copilot_gateway.models.request import ToolDef
from copilot_gateway.provider.prompt import NormalizedMessage, NormalizedToolCall
from copilot_gateway.provider.tools import (
    build_tools_preamble,
    compile_sydney_turn,
    parse_tool_calls,
    strip_tool_calls_block,
    tools_active,
)


def make_tools():
    return [
        ToolDef(
            function={
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ),
        ToolDef(function={"name": "run_shell", "description": "Run a command"}),
    ]


def test_preamble_lists_tools_and_format():
    preamble = build_tools_preamble(make_tools(), "auto")

    assert "read_file" in preamble
    assert "run_shell" in preamble
    assert '"path"' in preamble
    assert "```tool_calls" in preamble
    assert "MUST" not in preamble


def test_preamble_required_choice():
    preamble = build_tools_preamble(make_tools(), "required")

    assert "MUST call at least one function" in preamble


def test_preamble_specific_function_choice():
    preamble = build_tools_preamble(
        make_tools(), {"type": "function", "function": {"name": "read_file"}}
    )

    assert "MUST call the function `read_file`" in preamble


def test_tools_active_respects_none_choice():
    tools = make_tools()

    assert tools_active(tools, "auto") is True
    assert tools_active(tools, None) is True
    assert tools_active(tools, "none") is False
    assert tools_active(None, "auto") is False
    assert tools_active([], "required") is False


def test_parse_tool_calls_valid():
    text = (
        'Here you go:\n```tool_calls\n'
        '[{"name": "read_file", "arguments": {"path": "a.py"}}, '
        '{"name": "run_shell", "arguments": {}}]\n```\nDone.'
    )

    calls = parse_tool_calls(text, make_tools())

    assert calls is not None
    assert [(c.name, c.arguments) for c in calls] == [
        ("read_file", {"path": "a.py"}),
        ("run_shell", {}),
    ]


def test_parse_tool_calls_rejects_unknown_function():
    text = '```tool_calls\n[{"name": "delete_all", "arguments": {}}]\n```'

    assert parse_tool_calls(text, make_tools()) is None


def test_parse_tool_calls_rejects_non_object_arguments():
    text = '```tool_calls\n[{"name": "read_file", "arguments": "a.py"}]\n```'

    assert parse_tool_calls(text, make_tools()) is None


def test_parse_tool_calls_rejects_bad_json():
    text = "```tool_calls\nnot json at all\n```"

    assert parse_tool_calls(text, make_tools()) is None


def test_parse_tool_calls_absent_block():
    assert parse_tool_calls("Just a normal answer.", make_tools()) is None


def test_strip_tool_calls_block_keeps_prose():
    text = "Working on it.\n```tool_calls\n[]\n```\nAll done."

    assert strip_tool_calls_block(text, make_tools()) == "Working on it.\n\nAll done."


def test_compile_first_turn_includes_system_history_and_preamble():
    messages = [
        NormalizedMessage(role="system", text="Be concise."),
        NormalizedMessage(role="user", text="List files"),
        NormalizedMessage(role="assistant", text="Here they are: a.py"),
        NormalizedMessage(role="user", text="Read a.py"),
    ]

    turn = compile_sydney_turn(
        messages,
        pending=messages[1:],
        tools=make_tools(),
        tool_choice="auto",
        is_first=True,
        max_inline_file_chars=1000,
    )

    assert "SYSTEM" in turn
    assert "Be concise." in turn
    assert "read_file" in turn  # the preamble
    assert "USER:\nList files" in turn
    assert "ASSISTANT:\nHere they are: a.py" in turn
    assert "Reply only to the last USER message." in turn


def test_compile_first_turn_without_tools_has_no_preamble():
    messages = [NormalizedMessage(role="user", text="hello")]

    turn = compile_sydney_turn(
        messages,
        pending=messages,
        tools=None,
        tool_choice=None,
        is_first=True,
        max_inline_file_chars=1000,
    )

    assert "```tool_calls" not in turn
    assert "USER:\nhello" in turn


def test_compile_follow_up_sends_only_pending_plus_tool_context():
    tool_call = NormalizedToolCall(id="call_1", name="read_file", arguments='{"path": "a.py"}')
    messages = [
        NormalizedMessage(role="user", text="Read a.py"),
        NormalizedMessage(role="assistant", text="", tool_calls=(tool_call,)),
        NormalizedMessage(
            role="tool", text="print('hi')", tool_call_id="call_1", tool_name="read_file"
        ),
    ]

    turn = compile_sydney_turn(
        messages,
        pending=[messages[2]],
        tools=make_tools(),
        tool_choice="auto",
        is_first=False,
        max_inline_file_chars=1000,
    )

    # The earlier user turn is already in the server-side conversation.
    assert "USER:\nRead a.py" not in turn
    # But the assistant's tool call gives the result meaning.
    assert "call_1" in turn and "read_file" in turn
    assert "TOOL RESULT [call_1] (read_file):\nprint('hi')" in turn
    assert "Continue the task" in turn


def test_compile_follow_up_with_new_user_message():
    messages = [
        NormalizedMessage(role="user", text="first"),
        NormalizedMessage(role="user", text="second"),
    ]

    turn = compile_sydney_turn(
        messages,
        pending=[messages[1]],
        tools=None,
        tool_choice=None,
        is_first=False,
        max_inline_file_chars=1000,
    )

    assert "USER:\nfirst" not in turn
    assert "USER:\nsecond" in turn
    assert "Continue the task" not in turn


@pytest.mark.parametrize(
    "text",
    [
        # Arguments sent as a JSON string, as OpenAI clients do.
        '```tool_calls\n[{"name": "read_file", "arguments": "{\\"path\\": \\"a.py\\"}"}]\n```',
        # OpenAI nesting: the call sits under "function".
        '```tool_calls\n[{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]\n```',
        # A single object instead of an array.
        '```tool_calls\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```',
        # A json fence or a singular tag instead of tool_calls.
        '```json\n[{"name": "read_file", "arguments": {"path": "a.py"}}]\n```',
        '```tool_call\n[{"name": "read_file", "arguments": {"path": "a.py"}}]\n```',
        # No newline between the tag and the block.
        '```tool_calls [{"name": "read_file", "arguments": {"path": "a.py"}}]```',
        # Stray whitespace around the tool name.
        '```tool_calls\n[{"name": " read_file ", "arguments": {"path": "a.py"}}]\n```',
    ],
)
def test_parse_tool_calls_accepts_common_model_variants(text):
    calls = parse_tool_calls(text, make_tools())

    assert [(c.name, c.arguments) for c in calls] == [("read_file", {"path": "a.py"})]


def test_json_example_that_is_not_a_tool_call_stays_as_text():
    text = 'Example config:\n```json\n{"name": "app", "port": 8000}\n```'

    assert parse_tool_calls(text, make_tools()) is None
    assert strip_tool_calls_block(text, make_tools()) == text
