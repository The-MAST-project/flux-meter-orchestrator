from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

LOCALHOST = "127.0.0.1"
STARTUP_TIMEOUT_S = 20.0
POLL_INTERVAL_S = 0.1


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((LOCALHOST, 0))
        return sock.getsockname()[1]


def service_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "flux_meter_service", "--host", LOCALHOST, *args]


@pytest.fixture
def fake_service() -> Iterator[str]:
    """The service under uvicorn with `--fake`, as `host:port`."""
    port = free_port()
    process = subprocess.Popen(service_command("--port", str(port), "--fake"), stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while True:
        with socket.socket() as sock:
            if sock.connect_ex((LOCALHOST, port)) == 0:
                break
        if process.poll() is not None or time.monotonic() > deadline:
            process.kill()
            _, stderr = process.communicate()
            raise RuntimeError(f"the fake service did not start: {stderr}")
        time.sleep(POLL_INTERVAL_S)
    try:
        yield f"{LOCALHOST}:{port}"
    finally:
        process.terminate()
        process.communicate(timeout=STARTUP_TIMEOUT_S)
