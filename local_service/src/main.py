from __future__ import annotations

import asyncio
import logging
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from . import runtime, tools
from .logging_setup import setup_logging
from .routes import router
from .wake import WakeDetector

log = logging.getLogger("friday.main")

PORT_FILE = Path.home() / "Library" / "Application Support" / "Friday" / "port"


@asynccontextmanager
async def lifespan(app: FastAPI):
    tools.load_all()
    loop = asyncio.get_running_loop()
    detector = WakeDetector(loop)
    detector.start()
    runtime.detector = detector
    port = getattr(app.state, "bound_port", None)
    if port is not None:
        _write_port_file(port)
        log.info("local_service ready on 127.0.0.1:%d (wrote %s)", port, PORT_FILE)
    else:
        log.info("local_service ready")
    try:
        yield
    finally:
        log.info("shutting down wake detector")
        detector.stop()
        runtime.detector = None
        _clear_port_file()


def create_app() -> FastAPI:
    app = FastAPI(title="Friday local_service", lifespan=lifespan)
    app.include_router(router)
    return app


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_port_file(port: int) -> None:
    PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORT_FILE.write_text(str(port))


def _clear_port_file() -> None:
    try:
        PORT_FILE.unlink()
    except FileNotFoundError:
        pass


def main() -> None:
    setup_logging()
    port = _pick_port()
    log.info("local_service binding 127.0.0.1:%d", port)
    app = create_app()
    app.state.bound_port = port
    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=port,
            log_config=None,
        )
    finally:
        _clear_port_file()


if __name__ == "__main__":
    main()
