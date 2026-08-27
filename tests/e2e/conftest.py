"""A real browser against a real server.

The integration suite drives the application through ``TestClient``, which is
fast and proves the routes work. It cannot prove that a form the analyst
actually fills in submits the fields the route expects, that a redirect lands
somewhere useful, or that the page a person reads says what we think it says.
That is what these do.

Each run gets its own migrated, seeded database in a temporary directory, and
its own uvicorn process on a free port. Nothing here touches the repository's
``reconcile.db``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

PERIOD_START = "2025-07-01"
PERIOD_END = "2025-07-07"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_serving(url: str, process: subprocess.Popen[bytes], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited with {process.returncode} before serving")
        try:
            with urlopen(url, timeout=1):
                return
        except (URLError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"server did not answer {url} within {timeout}s")


@pytest.fixture(scope="session")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A migrated, seeded application serving on its own port."""
    workdir = tmp_path_factory.mktemp("e2e")
    database_url = f"sqlite:///{workdir / 'e2e.db'}"
    env = {**os.environ, "DATABASE_URL": database_url, "PYTHONPATH": str(ROOT)}

    for command in (
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [sys.executable, "-m", "app.seed"],
    ):
        done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError(f"{command[-1]} failed:\n{done.stdout}\n{done.stderr}")

    port = _free_port()
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_serving(url, server)
        yield url
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
