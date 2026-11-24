"""Loopback MCP fixture shared by framework and robot tests."""

import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn


@contextmanager
def run_mcp(mcp):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(mcp.streamable_http_app(), log_level="warning")
        )
        worker = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                assert time.monotonic() < deadline and worker.is_alive()
                time.sleep(0.01)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            server.should_exit = True
            worker.join(5)
            assert not worker.is_alive()


@pytest.fixture
def serve_mcp():
    """Serve a FastMCP instance on a loopback port."""
    return run_mcp
