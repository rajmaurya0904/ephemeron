"""The ``ephemeron`` command line."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .box import (
    Box,
    BoxError,
    Manager,
    Request,
    probe_host,
    ssh_command,
    state_root,
)
from .docker import Backend, Docker, DockerCLI, DockerError, DockerUnavailable
from .keys import KeyFileError
from .provision import BoxSpec, describe_command, image_tag, render_dockerfile, run_args
from .toolchains import TOOLCHAINS, ToolchainError, resolve_toolchain, validate_image

_DURATION = re.compile(r"^(?P<n>\d+(?:\.\d+)?)(?P<unit>[smhd]?)$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> float:
    """``90``, ``90s``, ``15m``, ``2h`` or ``1d`` to seconds."""
    match = _DURATION.match(text.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(f"'{text}' is not a duration (try 90s, 15m, 2h, 1d)")
    return float(match.group("n")) * _UNITS[match.group("unit")]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ephemeron",
        description="Start a throwaway container with a toolchain and a temporary SSH endpoint; "
        "destroy it when the last session disconnects.",
    )
    parser.add_argument("--version", action="version", version=f"ephemeron {__version__}")
    parser.add_argument(
        "--docker-bin", default="docker", metavar="PATH", help="docker client to use"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    run = sub.add_parser(
        "run",
        help="start a box and wait for sessions",
        description="Start a throwaway box. Arguments after '--' are run over SSH when "
        "--connect is given.",
    )
    run.add_argument(
        "toolchain",
        nargs="?",
        help="python, node:20, go, rust, ... (see 'ephemeron toolchains'); or use --image",
    )
    run.add_argument("--image", help="use this image instead of a named toolchain")
    run.add_argument(
        "-k",
        "--authorized-key",
        action="append",
        default=[],
        type=Path,
        metavar="FILE",
        help="public key file allowed to log in (repeatable); default: make a throwaway keypair",
    )
    run.add_argument("--user", default="dev", help="login user (default: dev; 'root' allowed)")
    run.add_argument("--bind", default="127.0.0.1", help="host address to publish SSH on")
    run.add_argument("--port", type=int, help="host port (default: a free one)")
    run.add_argument(
        "--ttl", type=parse_duration, default=3600.0, metavar="DURATION",
        help="hard lifetime limit (default: 1h)",
    )
    run.add_argument(
        "--grace", type=parse_duration, default=10.0, metavar="DURATION",
        help="wait this long after the last disconnect before destroying (default: 10s)",
    )
    run.add_argument(
        "--first-connect", type=parse_duration, default=600.0, metavar="DURATION",
        help="give up if nobody connects within this long (default: 10m)",
    )
    run.add_argument("--poll", type=parse_duration, default=2.0, metavar="DURATION",
                     help="how often to count sessions (default: 2s)")
    run.add_argument("--memory", default="2g", help="memory limit, '' for none (default: 2g)")
    run.add_argument("--cpus", help="CPU limit, e.g. 1.5")
    run.add_argument("--pids-limit", type=int, default=512, help="process limit (default: 512)")
    run.add_argument("-e", "--env", action="append", default=[], metavar="NAME=VALUE")
    run.add_argument(
        "--copy", action="append", default=[], metavar="SRC[=DEST]",
        help="copy a file or directory into the box (default dest: the login home)",
    )
    run.add_argument("--rebuild", action="store_true", help="rebuild the cached SSH image")
    run.add_argument("--connect", action="store_true",
                     help="open ssh yourself, then destroy the box when it exits")
    run.add_argument("--json", action="store_true", help="print the connection details as JSON")
    run.add_argument("--dry-run", action="store_true",
                     help="print the Dockerfile and docker command, start nothing")

    sub.add_parser("list", help="show boxes ephemeron created and when they expire")
    destroy = sub.add_parser("destroy", help="destroy named boxes now")
    destroy.add_argument("names", nargs="+")
    prune = sub.add_parser("prune", help="destroy expired boxes (and stale key material)")
    prune.add_argument("--all", action="store_true", help="destroy every ephemeron box")
    sub.add_parser("toolchains", help="list the built-in toolchains")
    return parser


def split_remote_command(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    argv = list(argv)
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1 :]
    return argv, []


def request_from_args(args: argparse.Namespace) -> Request:
    from .provision import parse_env

    if bool(args.toolchain) == bool(args.image):
        raise ToolchainError("give exactly one of a toolchain name or --image")
    if args.image:
        image, label = validate_image(args.image), args.image
    else:
        image, label = resolve_toolchain(args.toolchain), args.toolchain
    if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", args.user):
        raise ToolchainError(f"'{args.user}' is not a valid login name")
    if args.port is not None and not 1 <= args.port <= 65535:
        raise ToolchainError("--port must be between 1 and 65535")
    try:
        env = parse_env(args.env)
    except ValueError as exc:
        raise ToolchainError(str(exc)) from exc
    copies: list[tuple[str, str | None]] = []
    for item in args.copy:
        source, _, dest = item.partition("=")
        copies.append((source, dest or None))
    return Request(
        base_image=image,
        toolchain=label,
        key_files=list(args.authorized_key),
        user=args.user,
        bind=args.bind,
        port=args.port,
        lifetime=args.ttl,
        grace=args.grace,
        first_connect=args.first_connect,
        poll=args.poll,
        memory=args.memory or None,
        cpus=args.cpus,
        pids_limit=args.pids_limit or None,
        env=env,
        copies=copies,
        rebuild=args.rebuild,
    )


def dry_run(request: Request, out: Callable[[str], None]) -> None:
    tag = image_tag(request.base_image, request.user)
    out(f"# image tag: {tag}")
    out(render_dockerfile(request.base_image, request.user).rstrip())
    spec = BoxSpec(
        name="ephemeron-XXXXXX",
        image=tag,
        toolchain=request.toolchain,
        authorized_keys=["<public key>"],
        user=request.user,
        bind=request.bind,
        host_port=request.port,
        lifetime=request.lifetime,
        memory=request.memory,
        cpus=request.cpus,
        pids_limit=request.pids_limit,
        env=request.env,
    )
    out("# run:")
    out(describe_command(run_args(spec, now=0)))


def announce(box: Box, as_json: bool, out: Callable[[str], None]) -> None:
    command = ssh_command(box)
    if as_json:
        out(
            json.dumps(
                {
                    "name": box.name,
                    "host": probe_host(box.request.bind),
                    "port": box.port,
                    "user": box.user,
                    "identity": str(box.private_key) if box.private_key else None,
                    "ssh": command,
                }
            )
        )
        return
    req = box.request
    out(f"{box.name} is up ({req.base_image}, user {box.user})")
    out("  connect:  " + " ".join(_quote(part) for part in command))
    out(
        f"  ends:     {_seconds(req.grace)} after the last session disconnects, "
        f"or after {_seconds(req.lifetime)} at the latest"
    )
    if req.bind not in ("127.0.0.1", "localhost", "::1"):
        out(f"  note:     published on {req.bind}; anyone with the key can reach it")
    out(f"  waiting for the first connection (gives up after {_seconds(req.first_connect)})")


def _quote(part: str) -> str:
    return f'"{part}"' if " " in part else part


def _seconds(value: float) -> str:
    value = float(value)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if value >= size and value % size == 0:
            return f"{int(value // size)}{unit}"
    return f"{value:g}s"


def run_connect(box: Box, remote: list[str], out: Callable[[str], None]) -> int:
    if shutil.which("ssh") is None:
        raise BoxError("--connect needs the 'ssh' client on PATH")
    if box.private_key is None:
        raise BoxError("--connect only works with the throwaway keypair (omit --authorized-key)")
    return subprocess.call([*ssh_command(box), *remote])


def _terminate(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def cmd_run(args: argparse.Namespace, remote: list[str], manager: Manager) -> int:
    request = request_from_args(args)
    if args.dry_run:
        dry_run(request, manager.say)
        return 0
    if remote and not args.connect:
        raise ToolchainError("a command after '--' needs --connect")
    previous = signal.signal(signal.SIGTERM, _terminate)
    box = manager.create(request)
    code = 0
    try:
        announce(box, args.json, manager.say)
        if args.connect:
            code = run_connect(box, remote, manager.say)
            reason = "the ssh session ended"
        else:
            reason = manager.watch(box)
    except KeyboardInterrupt:
        reason, code = "interrupted", 130
    finally:
        removed = manager.destroy(box.name)
        signal.signal(signal.SIGTERM, previous)
    manager.say(f"{box.name} destroyed ({reason})" if removed else f"{box.name} was already gone")
    return code


def cmd_list(manager: Manager) -> int:
    rows = manager.docker.managed()
    if not rows:
        manager.say("no ephemeron boxes")
        return 0
    now = manager.clock()
    manager.say(f"{'NAME':<18} {'TOOLCHAIN':<14} {'STATE':<10} {'PORTS':<28} EXPIRES")
    for row in rows:
        labels = dict(
            part.split("=", 1) for part in (row.get("Labels") or "").split(",") if "=" in part
        )
        expires = labels.get("ephemeron.expires", "")
        left = "?"
        if expires.isdigit():
            remaining = int(expires) - now
            left = "expired" if remaining <= 0 else "in " + _seconds(round(remaining))
        manager.say(
            f"{row.get('Names', ''):<18} {labels.get('ephemeron.toolchain', ''):<14} "
            f"{row.get('State', ''):<10} {row.get('Ports', ''):<28} {left}"
        )
    return 0


def cmd_destroy(names: list[str], manager: Manager) -> int:
    status = 0
    for name in names:
        if not re.match(r"^ephemeron-[0-9a-f]{6}$", name):
            manager.say(f"{name}: not an ephemeron box name")
            status = 1
        elif manager.destroy(name):
            manager.say(f"{name} destroyed")
        else:
            manager.say(f"{name}: no such box")
            status = 1
    return status


def cmd_prune(everything: bool, manager: Manager) -> int:
    now = manager.clock()
    victims = []
    for row in manager.docker.managed():
        labels = dict(
            part.split("=", 1) for part in (row.get("Labels") or "").split(",") if "=" in part
        )
        expires = labels.get("ephemeron.expires", "0")
        if everything or not expires.isdigit() or int(expires) <= now:
            victims.append(row.get("Names", ""))
    for name in victims:
        manager.destroy(name)
        manager.say(f"destroyed {name}")
    root: Path = manager.root
    if root.is_dir():
        live = {row.get("Names", "") for row in manager.docker.managed()}
        for entry in root.iterdir():
            stale = manager.clock() - entry.stat().st_mtime > 600
            if entry.is_dir() and entry.name not in live and stale:
                shutil.rmtree(entry, ignore_errors=True)
                manager.say(f"removed stale key directory {entry.name}")
    if not victims:
        manager.say("nothing to prune")
    return 0


def cmd_toolchains(out: Callable[[str], None]) -> int:
    out(f"{'NAME':<8} {'DEFAULT IMAGE':<34} DESCRIPTION")
    for tc in TOOLCHAINS.values():
        out(f"{tc.name:<8} {tc.resolve():<34} {tc.description}")
    out("")
    out("Use name:version (python:3.11, node:20) or --image for anything else.")
    return 0


def main(
    argv: Sequence[str] | None = None,
    backend: Backend | None = None,
    manager: Manager | None = None,
) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    own, remote = split_remote_command(raw)
    parser = build_parser()
    args = parser.parse_args(own)
    if args.command == "toolchains":
        return cmd_toolchains(print)
    if manager is None:
        backend = backend or DockerCLI(args.docker_bin)
        manager = Manager(Docker(backend), state_root(), say=lambda m: print(m, flush=True))
    try:
        if args.command == "run":
            return cmd_run(args, remote, manager)
        if args.command == "list":
            return cmd_list(manager)
        if args.command == "destroy":
            return cmd_destroy(args.names, manager)
        if args.command == "prune":
            return cmd_prune(args.all, manager)
    except KeyboardInterrupt:
        return 130
    except (ToolchainError, KeyFileError) as exc:
        print(f"ephemeron: {exc}", file=sys.stderr)
        return 2
    except DockerUnavailable as exc:
        print(f"ephemeron: {exc}", file=sys.stderr)
        return 1
    except (BoxError, DockerError) as exc:
        print(f"ephemeron: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
