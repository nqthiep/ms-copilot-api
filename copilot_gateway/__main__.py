import uvicorn

from copilot_gateway.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "copilot_gateway.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
