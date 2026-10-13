from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from copilot_gateway.models.request import ToolDef
from copilot_gateway.provider.prompt import NormalizedMessage


@dataclass(frozen=True)
class TextDelta:
    """A piece of the assistant's reply text, in arrival order."""

    text: str


@dataclass(frozen=True)
class ToolCall:
    """One parsed function call the assistant wants the client to run."""

    id: str
    name: str
    arguments: str  # JSON object, serialized


@dataclass(frozen=True)
class ToolCallsEvent:
    """The assistant's turn ended with tool calls instead of (only) text."""

    calls: tuple[ToolCall, ...]


ProviderEvent = TextDelta | ToolCallsEvent


class ChatProvider(ABC):
    async def start(self) -> None:
        """Startup work, such as opening the browser."""

    async def stop(self) -> None:
        """Release resources."""

    @abstractmethod
    async def complete(self, session_key: str, messages: list[NormalizedMessage]) -> str:
        """Return the assistant's reply to the conversation's last user message."""

    async def run(
        self,
        session_key: str,
        messages: list[NormalizedMessage],
        *,
        tools: list[ToolDef] | None = None,
        tool_choice: object = None,
        model: str = "m365-copilot",
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one turn as events. The default wraps complete(); providers
        with real streaming (or tool support) override this."""
        _ = (tools, tool_choice, model)
        yield TextDelta(await self.complete(session_key, messages))

    def health(self) -> dict[str, object]:
        return {}
