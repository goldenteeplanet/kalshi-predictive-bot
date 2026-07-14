import logging

from rich.logging import RichHandler


def configure_logging(level: str = "INFO") -> None:
    """Configure readable console logging for CLI and jobs."""

    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
