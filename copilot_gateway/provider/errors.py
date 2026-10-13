class GatewayError(Exception):
    """Maps to an HTTP status and an OpenAI-style error body."""

    status_code = 500
    error_type = "server_error"


class BadRequest(GatewayError):
    status_code = 400
    error_type = "invalid_request_error"


class ProviderUnavailable(GatewayError):
    """Copilot is not usable right now, e.g. the user has not signed in."""

    status_code = 503
    error_type = "unavailable"


class ProviderTimeout(GatewayError):
    status_code = 504
    error_type = "timeout"
