import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from copilot_gateway.utils.logging import get_logger

log = get_logger("store")


@dataclass(frozen=True)
class ThreadState:
    # Copilot thread URL, or None when Copilot has not assigned one yet.
    thread_url: str | None = None
    # How many user turns the thread has received so far.
    user_turns: int = 0
    # Sydney (SignalR) conversation identifiers. None when the browser provider
    # or an older gateway version wrote the entry.
    sydney_session_id: str | None = None
    sydney_conversation_id: str | None = None
    # How many client turns (user + tool messages) the Sydney conversation holds.
    sydney_turns: int = 0


class SessionStore:
    """Maps a coding-agent session key to its Copilot thread, persisted as JSON."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict] | None = None

    def get(self, key: str) -> ThreadState | None:
        entry = self._load().get(key)
        if entry is None:
            return None
        try:
            return ThreadState(**entry)
        except TypeError:
            # Entry written by an older version of the gateway. Treat it as a new session.
            return None

    def put(self, key: str, state: ThreadState) -> None:
        self._load()[key] = asdict(state)
        self._flush()

    def forget(self, key: str) -> None:
        if self._load().pop(key, None) is not None:
            self._flush()

    def _load(self) -> dict[str, dict]:
        if self._data is None:
            self._data = {}
            if self._path.exists():
                try:
                    self._data = json.loads(self._path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    log.warning("Session store %s is unreadable; starting empty", self._path)
        return self._data

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
