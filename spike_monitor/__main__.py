"""Run the server: ``python -m spike_monitor``."""
import logging
import logging.handlers

import uvicorn

from .config import settings
from .main import create_app


def main() -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    # Also keep a log file, so problems can be found when running in the background.
    log_file = settings.data_dir / "server.log"
    handlers.append(logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3,
                                                         encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info", log_config=None)


if __name__ == "__main__":
    main()
