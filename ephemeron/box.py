"""Create, watch and destroy one throwaway SSH box."""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .docker import Docker, DockerError
from .keys import generate_keypair, read_public_keys
from .provision import (
    DEFAULT_USER,
    SSH_PORT,
    BoxSpec,
    home_for,
    image_tag,
    render_dockerfile,
    run_args,
)
from .sessions import LIST_PROCESSES, IdleTracker, count_sessions


class BoxError(Exception):
    """Something went wrong creating or running a box (message is user-facing)."""


def state_root() -> Path:
    """Where per-box key material lives (override with EPHEMERON_HOME)."""
    override = os.environ.get("EPHEMERON_HOME")
    return Path(override) if override else Path.home() / ".ephemeron"


def new_name() -> str:
    return "ephemeron-" + secrets.token_hex(3)


@dataclass
class Request:
    base_image: str
    toolchain: str
    key_files: list[Path] = field(default_factory=list)
    user: str = DEFAULT_USER
    bind: str = "127.0.0.1"
    port: int | None = None
    lifetime: float = 3600.0
    grace: float = 10.0
    first_connect: float = 600.0
    poll: float = 2.0
    memory: str | None = "2g"
    cpus: str | None = None
    pids_limit: int | None = 512
    env: list[tuple[str, str]] = field(default_factory=list)
    copies: list[tuple[str, str | None]] = field(default_factory=list)
    rebuild: bool = False


@dataclass
class Box:
    name: str
    host: str
    port: int
    user: str
    private_key: Path | None
    request: Request
    created: float


def probe_host(bind: str) -> str:
    return "127.0.0.1" if bind in ("0.0.0.0", "") else bind  # noqa: S104


def ssh_banner(host: str, port: int, timeout: float = 2.0) -> bool:
    """True when something on host:port answers with an SSH protocol banner."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as conn:
            conn.settimeout(timeout)
            return conn.recv(4) == b"SSH-"
    except OSError:
        return False


def ssh_command(box: Box, shown_host: str | None = None) -> list[str]:
    null = "NUL" if os.name == "nt" else "/dev/null"
    cmd = ["ssh"]
    if box.private_key is not None:
        cmd += ["-i", str(box.private_key), "-o", "IdentitiesOnly=yes"]
    cmd += [
        "-p",
        str(box.port),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"UserKnownHostsFile={null}",
        "-o",
        "LogLevel=ERROR",
        f"{box.user}@{shown_host or probe_host(box.request.bind)}",
    ]
    return cmd


class Manager:
    """Drives Docker to create, observe and remove boxes. Time and I/O are injectable."""

    def __init__(
        self,
        docker: Docker,
        root: Path | None = None,
        say: Callable[[str], None] = print,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        probe: Callable[[str, int], bool] = ssh_banner,
        ready_timeout: float = 45.0,
    ) -> None:
        self.docker = docker
        self.root = root if root is not None else state_root()
        self.say = say
        self.clock = clock
        self.sleep = sleep
        self.probe = probe
        self.ready_timeout = ready_timeout

    # -- create -----------------------------------------------------------------

    def ensure_image(self, request: Request) -> str:
        tag = image_tag(request.base_image, request.user)
        if not request.rebuild and self.docker.image_exists(tag):
            return tag
        self.say(f"building an SSH-enabled {request.base_image} image (cached after this run)...")
        try:
            self.docker.build(tag, render_dockerfile(request.base_image, request.user))
        except DockerError as exc:
            raise BoxError(
                f"could not build an SSH-enabled image from {request.base_image}: {exc}"
            ) from exc
        return tag

    def create(self, request: Request) -> Box:
        version = self.docker.check()
        self.say(f"docker {version} reachable")
        name = new_name()
        state_dir = self.root / name
        private_key: Path | None = None
        if request.key_files:
            keys = [key for path in request.key_files for key in read_public_keys(path)]
        else:
            pair = generate_keypair(state_dir, f"ephemeron-{name}")
            keys, private_key = [pair.public_key], pair.private_path
            if os.name != "nt":
                os.chmod(private_key, 0o600)
        try:
            image = self.ensure_image(request)
            spec = BoxSpec(
                name=name,
                image=image,
                toolchain=request.toolchain,
                authorized_keys=keys,
                user=request.user,
                bind=request.bind,
                host_port=request.port,
                lifetime=request.lifetime,
                memory=request.memory,
                cpus=request.cpus,
                pids_limit=request.pids_limit,
                env=request.env,
            )
            created = self.clock()
            try:
                self.docker.run_detached(run_args(spec, created))
            except DockerError as exc:
                raise BoxError(f"docker could not start the container: {exc}") from exc
            _, port = self.docker.published_port(name, SSH_PORT)
            box = Box(name, request.bind, port, request.user, private_key, request, created)
            self._wait_ready(box)
            self._copy_in(box)
            return box
        except BaseException:
            self.destroy(name)
            raise

    def _wait_ready(self, box: Box) -> None:
        deadline = self.clock() + self.ready_timeout
        host = probe_host(box.request.bind)
        while True:
            if not self.docker.is_running(box.name):
                raise BoxError(
                    "the container exited during start-up:\n" + self.docker.logs(box.name)
                )
            if self.probe(host, box.port):
                return
            if self.clock() >= deadline:
                raise BoxError(
                    f"sshd did not answer on {host}:{box.port} within "
                    f"{self.ready_timeout:g}s:\n" + self.docker.logs(box.name)
                )
            self.sleep(0.5)

    def _copy_in(self, box: Box) -> None:
        for source, destination in box.request.copies:
            src = Path(source)
            if not src.exists():
                raise BoxError(f"--copy source does not exist: {source}")
            target = destination or f"{home_for(box.user)}/{src.name}"
            try:
                self.docker.copy_in(box.name, str(src), target)
                self.docker.exec(box.name, ["chown", "-R", box.user, target], user="root")
            except DockerError as exc:
                raise BoxError(f"could not copy {source} into the box: {exc}") from exc
            self.say(f"copied {source} -> {target}")

    # -- watch --------------------------------------------------------------------

    def watch(self, box: Box) -> str:
        """Block until the box should be destroyed; return the reason."""
        req = box.request
        tracker = IdleTracker(box.created, req.lifetime, req.first_connect, req.grace)
        failures = 0
        while True:
            self.sleep(req.poll)
            now = self.clock()
            if not self.docker.is_running(box.name):
                return "the container stopped on its own"
            try:
                listing = self.docker.exec(box.name, ["sh", "-c", LIST_PROCESSES])
                failures = 0
            except DockerError as exc:
                failures += 1
                if failures >= 5:
                    raise BoxError(f"lost contact with the container: {exc}") from exc
                continue
            decision = tracker.update(count_sessions(listing), now)
            if decision.event:
                self.say(decision.event)
            if tracker.idle_since is not None and not decision.destroy:
                left = tracker.seconds_until_destroy(now)
                if left is not None and decision.event:
                    self.say(f"destroying in {left:g}s unless someone reconnects")
            if decision.destroy:
                return decision.reason

    # -- destroy ------------------------------------------------------------------

    def destroy(self, name: str) -> bool:
        """Remove the container and its key material. Safe to call twice."""
        removed = False
        try:
            removed = self.docker.remove(name)
        finally:
            shutil.rmtree(self.root / name, ignore_errors=True)
        return removed
