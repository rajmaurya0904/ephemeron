"""A stateful stand-in for the ``docker`` executable, with a real listening socket.

``run`` starts a background process that answers on a loopback port with an SSH banner, so
ephemeron's readiness probe is exercised for real. The number of logged-in sessions that
``exec`` reports is whatever the test last wrote into ``<state>/sessions``.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

STATE = Path(os.environ["FAKE_DOCKER_STATE"])
DB = STATE / "db.json"


def load() -> dict:
    return json.loads(DB.read_text()) if DB.exists() else {"images": [], "containers": {}}


def save(db: dict) -> None:
    DB.write_text(json.dumps(db))


def opt(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def serve(port_file: str) -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    Path(port_file).write_text(str(server.getsockname()[1]))
    while True:
        conn, _ = server.accept()
        try:
            conn.sendall(b"SSH-2.0-FakeSSH\r\n")
        finally:
            conn.close()


def spawn_server(name: str) -> tuple[int, int]:
    port_file = STATE / f"{name}.port"
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_GROUP
    else:
        flags["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, __file__, "--serve", str(port_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **flags,
    )
    for _ in range(100):
        if port_file.exists() and port_file.read_text():
            return proc.pid, int(port_file.read_text())
        time.sleep(0.05)
    raise SystemExit("fake server did not start")


def kill(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def fail(message: str) -> int:
    sys.stderr.write(message + "\n")
    return 1


def main(argv: list[str]) -> int:
    if os.environ.get("FAKE_DOCKER_DOWN"):
        return fail("Cannot connect to the Docker daemon. Is the docker daemon running?")
    db = load()
    verb = argv[0]

    if verb == "version":
        print("27.0.0")
    elif verb == "image":
        return 0 if argv[-1] in db["images"] else fail(f"Error: No such image: {argv[-1]}")
    elif verb == "build":
        db["images"].append(argv[argv.index("--tag") + 1])
        (STATE / "Dockerfile").write_text(sys.stdin.read())
        save(db)
    elif verb == "run":
        name = opt(argv, "--name")[0]
        labels = dict(item.split("=", 1) for item in opt(argv, "--label"))
        pid, port = spawn_server(name)
        db["containers"][name] = {"pid": pid, "port": port, "labels": labels, "argv": argv}
        save(db)
        print("deadbeef" * 8)
    elif verb == "port":
        print(f"127.0.0.1:{db['containers'][argv[1]]['port']}")
    elif verb == "inspect":
        if argv[-1] not in db["containers"]:
            return fail("Error: No such object")
        print("true")
    elif verb == "exec":
        if "chown" not in argv:
            sessions = STATE / "sessions"
            count = int(sessions.read_text() or 0) if sessions.exists() else 0
            for _ in range(count):
                print("sshd: dev [priv]")
    elif verb == "cp":
        pass
    elif verb == "logs":
        print("fake logs")
    elif verb == "rm":
        name = argv[-1]
        if name not in db["containers"]:
            return fail(f"Error response from daemon: No such container: {name}")
        kill(db["containers"].pop(name)["pid"])
        save(db)
    elif verb == "ps":
        for name, c in db["containers"].items():
            labels = ",".join(f"{k}={v}" for k, v in c["labels"].items())
            print(json.dumps({"Names": name, "State": "running", "Ports": "", "Labels": labels}))
    else:
        return fail(f"fake docker: unsupported {verb}")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--serve"]:
        serve(sys.argv[2])
    else:
        raise SystemExit(main(sys.argv[1:]))
