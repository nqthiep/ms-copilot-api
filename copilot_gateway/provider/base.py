from abc import ABC, abstractmethod

from copilot_gateway.provider.prompt import NormalizedMessage


class ChatProvider(ABC):
    async def start(self) -> None:
        """Startup work, such as opening the browser."""

    async def stop(self) -> None:
        """Release resources."""

    @abstractmethod
    async def complete(self, session_key: str, messages: list[NormalizedMessage]) -> str:
        """Return the assistant's reply to the conversation's last user message."""

    def health(self) -> dict[str, object]:
        return {}
