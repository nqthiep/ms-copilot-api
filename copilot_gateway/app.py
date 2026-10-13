from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from copilot_gateway.api.chat import router as chat_router
from copilot_gateway.config import Settings, load_settings
from copilot_gateway.provider.base import ChatProvider
from copilot_gateway.provider.copilot_provider import CopilotProvider
from copilot_gateway.provider.errors import GatewayError
from copilot_gateway.utils.logging import get_logger, setup_logging

log = get_logger("app")


def create_app(settings: Settings | None = None, provider: ChatProvider | None = None) -> FastAPI:
    settings = settings or load_settings()
    setup_logging()
    provider = provider or CopilotProvider(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("Starting gateway on %s:%d", settings.host, settings.port)
        log.info(
            "Inline limit %d characters; longer messages are attached as a file",
            settings.max_inline_chars,
        )
        await provider.start()
        try:
            yield
        finally:
            await provider.stop()

    app = FastAPI(title="Copilot Gateway", lifespan=lifespan)
    app.state.settings = settings
    app.state.provider = provider
    app.include_router(chat_router)

    @app.exception_handler(GatewayError)
    async def gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc), "type": exc.error_type}},
        )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", **provider.health()}

    return app


app = create_app()
