import logging
import re

# Defense in depth: the gateway never logs prompts, responses or credentials,
# but if a library ever echoes a header or cookie, mask it before it hits the log.
_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|cookie|set-cookie|password|token|api[_-]?key)(\s*[:=]\s*)\S+"
)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _SECRET_PATTERN.sub(r"\1\2[REDACTED]", record.getMessage())
        record.args = ()
        return True


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger("copilot_gateway")
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactFilter())
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"copilot_gateway.{name}")
