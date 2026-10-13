"""A mock started by the tests does not outlive a killed test run (suite rule 11)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import free_port

pytestmark = pytest.mark.integration

TESTS = Path(__file__).resolve().parent

PARENT = """
import sys, time
from pathlib import Path
sys.path.insert(0, {tests!r})
from conftest import start_mock, wait_until_listening
log = Path({log!r})
process = start_mock(log, {port})
wait_until_listening("127.0.0.1", {port}, process, log)
print(process.pid, flush=True)
time.sleep(600)  # a test run that is about to be killed
"""


def listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups")
def test_the_mock_exits_when_the_process_that_started_it_is_killed(tmp_path) -> None:
    port = free_port()
    script = PARENT.format(tests=str(TESTS), log=str(tmp_path / "mock.log"), port=port)
    parent = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    mock_pid = None
    try:
        line = parent.stdout.readline()
        assert line, parent.stderr.read()
        mock_pid = int(line)
        assert listening(port)
        parent.kill()  # SIGKILL: no finally blocks, no fixture teardown
        parent.wait(timeout=10)
        deadline = time.monotonic() + 15
        while listening(port) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not listening(port), "the mock outlived the process that started it"
    finally:
        if parent.poll() is None:
            parent.kill()
        if mock_pid is not None:  # clean up if the mock did survive
            try:
                os.killpg(mock_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
