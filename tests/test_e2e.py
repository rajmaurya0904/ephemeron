"""Run the real ``ephemeron`` process against tests/fake_docker.py.

Only the docker executable is a stand-in. The CLI, the subprocess layer, the readiness probe
(a real TCP connection that must receive an SSH banner) and the polling loop are all real.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.fakes import ed25519_line

HERE = Path(__file__).parent


def make_launcher(tmp_path: Path) -> Path:
    script = HERE / "fake_docker.py"
    if os.name == "nt":
        launcher = tmp_path / "fakedocker.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        launcher = tmp_path / "fakedocker"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


class Session:
    """A running ``ephemeron run`` with its output available line by line."""

    def __init__(self, tmp_path: Path, *extra: str, down: bool = False) -> None:
        self.state = tmp_path / "docker-state"
        self.state.mkdir()
        key = tmp_path / "contractor.pub"
        key.write_text(ed25519_line("contractor") + "\n")
        env = dict(
            os.environ,
            FAKE_DOCKER_STATE=str(self.state),
            EPHEMERON_HOME=str(tmp_path / "home"),
            PYTHONUNBUFFERED="1",
            PYTHONIOENCODING="utf-8",
        )
        if down:
            env["FAKE_DOCKER_DOWN"] = "1"
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ephemeron.cli",
                "--docker-bin",
                str(make_launcher(tmp_path)),
                "run",
                "python",
                "-k",
                str(key),
                *extra,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.seen: list[str] = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))

    def wait_for(self, text: str, timeout: float = 60.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None and self.lines.empty():
                    break
                continue
            self.seen.append(line)
            if text in line:
                return line
        raise AssertionError(f"never saw {text!r}; saw {self.seen!r}")

    def set_sessions(self, count: int) -> None:
        (self.state / "sessions").write_text(str(count))

    def db(self) -> dict:
        return json.loads((self.state / "db.json").read_text())

    def finish(self, timeout: float = 60.0) -> tuple[int, str]:
        code = self.proc.wait(timeout=timeout)
        return code, self.proc.stderr.read()


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture
def cleanup():
    sessions = []
    yield sessions.append
    for s in sessions:
        if s.proc.poll() is None:
            s.proc.kill()
        for c in (json.loads((s.state / "db.json").read_text())["containers"].values()
                  if (s.state / "db.json").exists() else []):
            subprocess.run(
                ["taskkill", "/F", "/PID", str(c["pid"])] if os.name == "nt"
                else ["kill", "-9", str(c["pid"])],
                capture_output=True,
            )


def test_box_lives_while_connected_and_dies_after_last_disconnect(tmp_path, cleanup):
    s = Session(tmp_path, "--poll", "1s", "--grace", "2s", "--first-connect", "60s")
    cleanup(s)
    up = s.wait_for("is up (python:3.12-slim, user dev)")
    assert up.startswith("ephemeron-")
    connect = s.wait_for("connect:")
    assert "-p " in connect and "dev@127.0.0.1" in connect
    container = next(iter(s.db()["containers"].values()))
    port = container["port"]
    assert port_open(port)  # the box really is listening

    s.set_sessions(1)
    s.wait_for("session connected (1 active)")
    s.set_sessions(2)
    s.wait_for("session connected (2 active)")
    s.set_sessions(1)
    s.wait_for("session closed (1 active)")
    assert s.db()["containers"], "must stay alive while one session remains"
    s.set_sessions(0)
    s.wait_for("session closed (0 active)")
    done = s.wait_for("destroyed")
    assert "the last session disconnected" in done

    code, _ = s.finish()
    assert code == 0
    assert s.db()["containers"] == {}
    time.sleep(0.5)
    assert not port_open(port)  # the fake container process was really killed


def test_nobody_connects_so_it_gives_up(tmp_path, cleanup):
    s = Session(tmp_path, "--poll", "1s", "--first-connect", "3s")
    cleanup(s)
    s.wait_for("is up")
    done = s.wait_for("destroyed")
    assert "nobody connected in time" in done
    code, _ = s.finish()
    assert code == 0 and s.db()["containers"] == {}


def test_image_is_built_with_the_sshd_dockerfile(tmp_path, cleanup):
    s = Session(tmp_path, "--poll", "1s", "--first-connect", "2s")
    cleanup(s)
    s.wait_for("destroyed")
    s.finish()
    dockerfile = (s.state / "Dockerfile").read_text()
    assert dockerfile.startswith("FROM python:3.12-slim") and "openssh-server" in dockerfile
    assert "building an SSH-enabled python:3.12-slim image" in "\n".join(s.seen)


def test_docker_down_fails_fast_with_advice(tmp_path, cleanup):
    s = Session(tmp_path, down=True)
    cleanup(s)
    code, err = s.finish()
    assert code == 1 and "Start Docker Desktop" in err
