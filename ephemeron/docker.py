"""Thin wrapper around the ``docker`` command line client.

All Docker access goes through a :class:`Backend` ("run these arguments, give me exit code,
stdout and stderr"). The real backend shells out to the ``docker`` binary; tests plug in a
fake one.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_TIMEOUT = 60.0
BUILD_TIMEOUT = 1800.0

_DAEMON_DOWN_MARKERS = (
    "cannot connect to the docker daemon",
    "error during connect",
    "is the docker daemon running",
    "docker daemon is not running",
    "failed to connect to the docker api",
    "the system cannot find the file specified",
)


class DockerError(Exception):
    """A docker command ran and failed, or could not complete."""

    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class DockerUnavailable(DockerError):
    """Docker is not installed or its daemon is not reachable."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Backend(Protocol):
    def run(
        self,
        args: Sequence[str],
        timeout: float | None = None,
        input_text: str | None = None,
    ) -> CommandResult: ...


class DockerCLI:
    """Runs ``docker <args>`` as a subprocess (never through a shell)."""

    def __init__(self, program: str = "docker", timeout: float = DEFAULT_TIMEOUT) -> None:
        self.program = program
        self.timeout = timeout

    def run(
        self,
        args: Sequence[str],
        timeout: float | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        found = shutil.which(self.program)
        if found is None:
            raise DockerUnavailable(
                f"The '{self.program}' executable was not found on PATH. Install Docker "
                "(Docker Desktop on Windows/macOS) or pass --docker-bin."
            )
        limit = self.timeout if timeout is None else timeout
        try:
            proc = subprocess.run(
                [found, *args],
                capture_output=True,
                timeout=limit,
                input=None if input_text is None else input_text.encode("utf-8"),
                stdin=None if input_text is not None else subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"docker {args[0]} timed out after {limit:g}s") from exc
        except OSError as exc:
            raise DockerUnavailable(f"Could not start docker: {exc}") from exc
        return CommandResult(
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )


def _daemon_down(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _DAEMON_DOWN_MARKERS)


_PORT_RE = re.compile(r"^(?P<host>\[[^\]]+\]|[^:]+):(?P<port>\d+)$")


def parse_published_port(output: str) -> tuple[str, int]:
    """Parse ``docker port`` output such as ``127.0.0.1:49153`` (first IPv4 line wins)."""
    candidates: list[tuple[str, int]] = []
    for line in output.splitlines():
        match = _PORT_RE.match(line.strip())
        if match:
            host = match.group("host")
            candidates.append((host, int(match.group("port"))))
    if not candidates:
        raise DockerError(f"Could not read the published SSH port from: {output.strip()!r}")
    for host, port in candidates:
        if not host.startswith("["):
            return host, port
    return candidates[0]


class Docker:
    """The handful of Docker operations ephemeron needs."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    def _run(
        self,
        args: Sequence[str],
        timeout: float | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        result = self.backend.run(args, timeout, input_text)
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip() or "docker exited with an error"
            if _daemon_down(message):
                raise DockerUnavailable(
                    "The Docker daemon is not reachable. Start Docker Desktop (or the docker "
                    f"service) and try again. Details: {message.splitlines()[0]}",
                    result.returncode,
                )
            raise DockerError(message, result.returncode)
        return result

    def check(self) -> str:
        """Return the daemon version, or raise if Docker cannot be reached."""
        out = self._run(["version", "--format", "{{.Server.Version}}"]).stdout.strip()
        return out or "unknown"

    def image_exists(self, tag: str) -> bool:
        result = self.backend.run(["image", "inspect", "--format", "{{.Id}}", tag], None, None)
        if result.returncode == 0:
            return True
        if _daemon_down(result.stderr):
            self._run(["image", "inspect", tag])  # raises DockerUnavailable with advice
        return False

    def build(self, tag: str, dockerfile: str) -> None:
        self._run(["build", "--tag", tag, "-"], timeout=BUILD_TIMEOUT, input_text=dockerfile)

    def run_detached(self, args: Sequence[str]) -> str:
        return self._run(["run", "--detach", *args]).stdout.strip()

    def published_port(self, name: str, container_port: int = 22) -> tuple[str, int]:
        out = self._run(["port", name, f"{container_port}/tcp"]).stdout
        return parse_published_port(out)

    def is_running(self, name: str) -> bool:
        result = self.backend.run(["inspect", "--format", "{{.State.Running}}", name], None, None)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def exec(self, name: str, command: Sequence[str], user: str | None = None) -> str:
        args = ["exec"]
        if user:
            args += ["--user", user]
        return self._run([*args, name, *command]).stdout

    def copy_in(self, name: str, source: str, destination: str) -> None:
        self._run(["cp", source, f"{name}:{destination}"])

    def logs(self, name: str, tail: int = 30) -> str:
        result = self.backend.run(["logs", "--tail", str(tail), name], None, None)
        return (result.stdout + result.stderr).strip()

    def remove(self, name: str) -> bool:
        """Force-remove a container. Returns False if it was already gone."""
        result = self.backend.run(["rm", "--force", "--volumes", name], None, None)
        if result.returncode == 0:
            return True
        message = (result.stderr or result.stdout).lower()
        if "no such container" in message:
            return False
        if _daemon_down(message):
            raise DockerUnavailable("The Docker daemon is not reachable while removing " + name)
        raise DockerError((result.stderr or result.stdout).strip(), result.returncode)

    def managed(self) -> list[dict[str, Any]]:
        """All containers created by ephemeron, running or not."""
        out = self._run(
            ["ps", "--all", "--no-trunc", "--filter", "label=ephemeron=1", "--format", "{{json .}}"]
        ).stdout
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    def label(self, name: str, key: str) -> str:
        fmt = '{{index .Config.Labels "' + key + '"}}'
        return self._run(["inspect", "--format", fmt, name]).stdout.strip()
