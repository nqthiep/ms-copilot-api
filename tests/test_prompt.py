import base64

import pytest

from copilot_gateway.models.request import ChatMessage
from copilot_gateway.provider.errors import BadRequest
from copilot_gateway.provider.prompt import (
    AttachedFile,
    NormalizedMessage,
    compile_prompt,
    normalize_messages,
    pending_user_turns,
)


def b64(data: bytes) -> str:
    return "data:text/plain;base64," + base64.b64encode(data).decode()


def test_compile_prompt_follows_the_template():
    messages = [
        NormalizedMessage("system", "Be brief."),
        NormalizedMessage("user", "hi"),
        NormalizedMessage("assistant", "hello"),
        NormalizedMessage("user", "what is 2+2?"),
    ]

    assert compile_prompt(messages) == (
        "SYSTEM\n\nBe brief.\n\n"
        "CONVERSATION\n\n"
        "USER:\nhi\n\nASSISTANT:\nhello\n\nUSER:\nwhat is 2+2?\n\n"
        "Reply only to the last USER message."
    )


def test_compile_prompt_without_system_message_has_no_system_block():
    prompt = compile_prompt([NormalizedMessage("user", "hi")])

    assert prompt.startswith("CONVERSATION\n\nUSER:\nhi")
    assert "SYSTEM" not in prompt


def test_content_parts_are_split_into_text_and_decoded_files():
    message = ChatMessage.model_validate(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "review this"},
                {"type": "file", "file": {"filename": "../a.py", "file_data": b64(b"print(1)")}},
            ],
        }
    )

    (normalized,) = normalize_messages([message], max_file_bytes=1000)

    assert normalized.text == "review this"
    assert normalized.files == (AttachedFile(filename="a.py", data=b"print(1)"),)


def test_developer_role_is_treated_as_system():
    message = ChatMessage.model_validate({"role": "developer", "content": "rules"})

    (normalized,) = normalize_messages([message], max_file_bytes=1000)

    assert normalized.role == "system"


@pytest.mark.parametrize(
    ("filename", "file_data", "message_part"),
    [
        ("tool.exe", b64(b"MZ"), "Unsupported file type"),
        ("notes.txt", "data:text/plain;base64,!!!not-base64!!!", "not valid base64"),
        ("big.txt", b64(b"x" * 20), "byte limit"),
    ],
)
def test_invalid_files_are_rejected(filename, file_data, message_part):
    message = ChatMessage.model_validate(
        {"role": "user", "content": [{"type": "file", "file": {"filename": filename, "file_data": file_data}}]}
    )

    with pytest.raises(BadRequest, match=message_part):
        normalize_messages([message], max_file_bytes=10)


def test_pending_turns_are_the_user_messages_after_those_already_sent():
    messages = [
        NormalizedMessage("user", "a"),
        NormalizedMessage("assistant", "b"),
        NormalizedMessage("user", "c"),
        NormalizedMessage("assistant", "d"),
        NormalizedMessage("user", "e"),
    ]

    assert pending_user_turns(messages, 2) == [NormalizedMessage("user", "e")]
    assert pending_user_turns(messages, 1) == [NormalizedMessage("user", "c"), NormalizedMessage("user", "e")]
    assert pending_user_turns(messages, 0) == [NormalizedMessage("user", t) for t in ("a", "c", "e")]


def test_stateless_client_sends_its_newest_message_every_time():
    newest = NormalizedMessage("user", "again")

    assert pending_user_turns([newest], 5) == [newest]
