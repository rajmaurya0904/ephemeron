"""Scripted stand-ins for the docker CLI and for time, used across the test-suite."""

from __future__ import annotations

import json
from collections.abc import Sequence

from ephemeron.docker import CommandResult


class FakeClock:
    """Time that only moves when something sleeps."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def process_listing(sessions: int, preauth: int = 0) -> str:
    """What `ps`-style output from the container looks like with N logins."""
    lines = ["sshd: /usr/sbin/sshd -D -e -o PasswordAuthentication=no", "sh -c sleep"]
    for _ in range(sessions):
        lines += ["sshd: dev [priv]", "sshd: dev@pts/0"]
    lines += ["sshd: [accepted]"] * preauth
    return "\n".join(lines) + "\n"


class FakeDocker:
    """Behaves like a healthy Docker host for the commands ephemeron issues."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.images: set[str] = set()
        self.containers: dict[str, dict] = {}
        self.session_counts: list[int] = []  # consumed one per process listing; last repeats
        self.fail: dict[str, CommandResult] = {}
        self.host_port = 49153
        self.daemon_down = False
        self.logs_text = "sshd: no hostkeys available -- exiting."
        self.running_after_run = True
        self.clock = clock

    # -- helpers ----------------------------------------------------------------

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]

    def _opt(self, args: list[str], flag: str) -> list[str]:
        return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]

    # -- the backend protocol ------------------------------------------------------

    def run(
        self,
        args: Sequence[str],
        timeout: float | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        args = list(args)
        self.calls.append(args)
        self.inputs.append(input_text)
        verb = args[0]
        if self.daemon_down:
            return CommandResult(1, "", "Cannot connect to the Docker daemon. Is it running?")
        key = f"{verb} {args[1]}" if verb in {"image"} else verb
        if key in self.fail:
            return self.fail[key]

        if verb == "version":
            return CommandResult(0, "27.3.1\n", "")
        if verb == "image":
            tag = args[-1]
            return (
                CommandResult(0, "sha256:abc\n", "")
                if tag in self.images
                else CommandResult(1, "", f"Error: No such image: {tag}")
            )
        if verb == "build":
            self.images.add(args[args.index("--tag") + 1])
            return CommandResult(0, "built\n", "")
        if verb == "run":
            name = self._opt(args, "--name")[0]
            labels = dict(item.split("=", 1) for item in self._opt(args, "--label"))
            publish = self._opt(args, "--publish")[0]
            self.containers[name] = {
                "running": self.running_after_run,
                "labels": labels,
                "publish": publish,
                "args": args,
                "copied": [],
            }
            return CommandResult(0, "c0ffee" * 10 + "\n", "")
        if verb == "port":
            name = args[1]
            if name not in self.containers:
                return CommandResult(1, "", f"Error: No such container: {name}")
            bind = self.containers[name]["publish"].split(":")[0]
            return CommandResult(0, f"{bind}:{self.host_port}\n[::]:{self.host_port}\n", "")
        if verb == "inspect":
            name = args[-1]
            if name not in self.containers:
                return CommandResult(1, "", f"Error: No such object: {name}")
            return CommandResult(0, "true\n" if self.containers[name]["running"] else "false\n", "")
        if verb == "exec":
            return self._exec(args)
        if verb == "cp":
            container = args[2].split(":", 1)[0]
            self.containers[container]["copied"].append(args[1:])
            return CommandResult(0, "", "")
        if verb == "logs":
            return CommandResult(0, self.logs_text + "\n", "")
        if verb == "rm":
            name = args[-1]
            if name not in self.containers:
                message = f"Error response from daemon: No such container: {name}"
                return CommandResult(1, "", message)
            del self.containers[name]
            return CommandResult(0, name + "\n", "")
        if verb == "ps":
            return CommandResult(0, self._ps(), "")
        return CommandResult(1, "", f"fake docker: unsupported {verb}")

    def _exec(self, args: list[str]) -> CommandResult:
        if "chown" in args:
            return CommandResult(0, "", "")
        if len(self.session_counts) > 1:
            count = self.session_counts.pop(0)
        else:
            count = self.session_counts[0] if self.session_counts else 0
        return CommandResult(0, process_listing(count), "")

    def _ps(self) -> str:
        rows = []
        for name, c in self.containers.items():
            labels = ",".join(f"{k}={v}" for k, v in c["labels"].items())
            rows.append(
                {
                    "Names": name,
                    "State": "running" if c["running"] else "exited",
                    "Ports": c["publish"],
                    "Labels": labels,
                }
            )
        return "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else "")


def ed25519_line(comment: str = "alice@laptop") -> str:
    import base64
    import struct

    kind = b"ssh-ed25519"
    blob = struct.pack(">I", len(kind)) + kind + struct.pack(">I", 32) + bytes(range(32))
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".strip()
