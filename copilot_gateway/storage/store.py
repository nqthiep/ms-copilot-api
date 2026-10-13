import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from copilot_gateway.utils.logging import get_logger

log = get_logger("store")


@dataclass(frozen=True)
class ThreadState:
    # Copilot thread URL, or None when Copilot has not assigned one yet.
    thread_url: str | None
    # Number of conversation messages (including the last assistant reply) that
    # the thread already contains, and a hash of them to detect divergence.
    message_count: int
    prefix_hash: str


class SessionStore:
    """Maps a coding-agent session key to its Copilot thread, persisted as JSON."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict] | None = None

    def get(self, key: str) -> ThreadState | None:
        entry = self._load().get(key)
        return ThreadState(**entry) if entry else None

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
