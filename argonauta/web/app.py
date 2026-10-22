"""Lifecycle of `argonauta ui`: context, server thread, browser, clean stop."""

from __future__ import annotations

import signal
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

from argonauta.logging_setup import get_logger
from argonauta.web.jobs import JobRunner
from argonauta.web.server import AppContext, create_server, new_token
from argonauta.web.terminal import TerminalManager
from argonauta.workspaces import RegistryError, open_registry

log = get_logger()
STATIC_DIR = Path(__file__).resolve().parent / "static"


def run_ui(host: str, port: int, open_browser: bool, start_repo: Path | None, announce: Callable[[str], None]) -> int:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the UI only listens on 127.0.0.1 or localhost")
    try:
        registry = open_registry()
        if start_repo is not None:
            registry.ensure_repo(start_repo)
    except RegistryError as exc:
        raise ValueError(str(exc)) from exc
    ctx = AppContext(registry=registry, jobs=JobRunner(), terminals=TerminalManager(), token=new_token(), static_dir=STATIC_DIR)
    server = create_server(ctx, host=host, port=port)
    url = f"http://{host}:{ctx.port}/#token={ctx.token}"
    announce(f"Argonauta UI: {url}")
    stop = threading.Event()

    def on_signal(signum, frame):  # noqa: ARG001
        stop.set()

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, name="argonauta-ui", daemon=True)
    thread.start()
    log.info("ui listening on port %s", ctx.port)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        server.shutdown()
        server.server_close()
        ctx.terminals.shutdown()
        ctx.jobs.shutdown()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        log.info("ui stopped")
    return 0
