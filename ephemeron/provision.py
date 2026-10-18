"""Build the SSH-enabled image and the ``docker run`` arguments for one throwaway box.

The image is the chosen toolchain image plus ``openssh-server`` and a login user. Host keys
and the authorized key are *not* baked in: they are created when the container starts, so
two boxes never share an identity and no secret lands in an image layer.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import time
from dataclasses import dataclass, field

LABEL = "ephemeron"
DEFAULT_USER = "dev"
SSH_PORT = 22
KEYS_ENV = "EPHEMERON_AUTHORIZED_KEYS"


def render_dockerfile(base_image: str, user: str = DEFAULT_USER) -> str:
    """Dockerfile that adds sshd to ``base_image`` on apt, apk or dnf/yum based distros."""
    make_user = ""
    if user != "root":
        make_user = (
            "if command -v useradd >/dev/null 2>&1; then "
            f"useradd --create-home --shell /bin/bash {user}; "
            "else "
            f"adduser -D -s /bin/bash {user}; "
            "fi; "
            # sshd refuses locked accounts; '*' means "no password, but not locked".
            f"echo '{user}:*' | chpasswd -e; "
        )
    install = (
        "if command -v apt-get >/dev/null 2>&1; then "
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update && "
        "apt-get install -y --no-install-recommends openssh-server && "
        "rm -rf /var/lib/apt/lists/*; "
        "elif command -v apk >/dev/null 2>&1; then "
        "apk add --no-cache openssh bash shadow; "
        "elif command -v dnf >/dev/null 2>&1; then "
        "dnf install -y openssh-server shadow-utils && dnf clean all; "
        "elif command -v yum >/dev/null 2>&1; then "
        "yum install -y openssh-server shadow-utils && yum clean all; "
        "else echo 'ephemeron: no supported package manager (apt, apk, dnf, yum)' >&2; exit 1; "
        "fi"
    )
    return (
        f"FROM {base_image}\n"
        f'LABEL {LABEL}.base="{base_image}"\n'
        f"RUN set -eu; {install}; {make_user}mkdir -p /run/sshd\n"
        f"EXPOSE {SSH_PORT}\n"
    )


def image_tag(base_image: str, user: str = DEFAULT_USER) -> str:
    """Stable tag derived from the base image and the Dockerfile text (so edits rebuild)."""
    digest = hashlib.sha256(render_dockerfile(base_image, user).encode()).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9_.-]+", "-", base_image.lower()).strip("-.")[:60]
    return f"ephemeron/{slug}:{digest}"


def home_for(user: str) -> str:
    return "/root" if user == "root" else f"/home/{user}"


def startup_script(user: str = DEFAULT_USER) -> str:
    """Shell run as PID 1: make host keys, install the authorized key, exec sshd."""
    home = home_for(user)
    ssh_dir = f"{home}/.ssh"
    sshd_opts = [
        "-D",
        "-e",
        "-o PasswordAuthentication=no",
        "-o KbdInteractiveAuthentication=no",
        "-o PubkeyAuthentication=yes",
        "-o PermitRootLogin=prohibit-password",
        f"-o AllowUsers={user}",
        "-o ClientAliveInterval=15",
        "-o ClientAliveCountMax=3",
        "-o PrintMotd=no",
    ]
    lines = [
        "set -eu",
        "mkdir -p /run/sshd",
        "ssh-keygen -A >/dev/null",
        f"mkdir -p {ssh_dir}",
        f'printf "%s\\n" "${KEYS_ENV}" > {ssh_dir}/authorized_keys',
        f"chmod 700 {ssh_dir}",
        f"chmod 600 {ssh_dir}/authorized_keys",
        f"chown -R {user} {ssh_dir}",
        f"unset {KEYS_ENV}",
        "exec /usr/sbin/sshd " + " ".join(sshd_opts),
    ]
    return "\n".join(lines)


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env(pairs: list[str]) -> list[tuple[str, str]]:
    result = []
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not _ENV_NAME.match(name):
            raise ValueError(f"--env expects NAME=value, got '{pair}'")
        if name == KEYS_ENV:
            raise ValueError(f"{KEYS_ENV} is reserved")
        result.append((name, value))
    return result


@dataclass
class BoxSpec:
    name: str
    image: str
    toolchain: str
    authorized_keys: list[str]
    user: str = DEFAULT_USER
    bind: str = "127.0.0.1"
    host_port: int | None = None
    lifetime: float = 3600.0
    memory: str | None = "2g"
    cpus: str | None = None
    pids_limit: int | None = 512
    env: list[tuple[str, str]] = field(default_factory=list)


def run_args(spec: BoxSpec, now: float | None = None) -> list[str]:
    """Arguments for ``docker run --detach`` (the caller adds the ``run --detach``)."""
    created = time.time() if now is None else now
    expires = int(created + spec.lifetime)
    publish = f"{spec.bind}:{spec.host_port or ''}:{SSH_PORT}"
    args = [
        "--name",
        spec.name,
        "--label",
        f"{LABEL}=1",
        "--label",
        f"{LABEL}.expires={expires}",
        "--label",
        f"{LABEL}.toolchain={spec.toolchain}",
        "--label",
        f"{LABEL}.user={spec.user}",
        "--publish",
        publish,
        "--env",
        f"{KEYS_ENV}={chr(10).join(spec.authorized_keys)}",
    ]
    for key, value in spec.env:
        args += ["--env", f"{key}={value}"]
    if spec.memory:
        args += ["--memory", spec.memory]
    if spec.cpus:
        args += ["--cpus", spec.cpus]
    if spec.pids_limit:
        args += ["--pids-limit", str(spec.pids_limit)]
    args += [spec.image, "sh", "-c", startup_script(spec.user)]
    return args


def describe_command(args: list[str]) -> str:
    """Human-readable ``docker run`` line (keys elided) for ``--dry-run``."""
    shown = []
    skip_next = False
    for item in args:
        if skip_next:
            shown.append(shlex.quote(item.split("=", 1)[0] + "=<elided>"))
            skip_next = False
        elif item == "--env":
            shown.append(item)
            skip_next = True
        else:
            shown.append(shlex.quote(item) if "\n" not in item else "'<startup script>'")
    return "docker run --detach " + " ".join(shown)
