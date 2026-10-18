import socket
import threading

import pytest

from ephemeron.box import (
    Box,
    BoxError,
    Manager,
    Request,
    new_name,
    probe_host,
    ssh_banner,
    ssh_command,
    state_root,
)
from ephemeron.docker import Docker
from ephemeron.provision import image_tag
from tests.fakes import FakeClock, FakeDocker, ed25519_line


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake(clock) -> FakeDocker:
    return FakeDocker(clock)


@pytest.fixture
def keyfile(tmp_path):
    path = tmp_path / "contractor.pub"
    path.write_text(ed25519_line("contractor") + "\n")
    return path


@pytest.fixture
def messages() -> list[str]:
    return []


@pytest.fixture
def manager(fake, clock, tmp_path, messages) -> Manager:
    return Manager(
        Docker(fake),
        root=tmp_path / "state",
        say=messages.append,
        clock=clock.time,
        sleep=clock.sleep,
        probe=lambda host, port: True,
        ready_timeout=5,
    )


def request(keyfile, **overrides) -> Request:
    params = dict(
        base_image="python:3.12-slim",
        toolchain="python",
        key_files=[keyfile],
        grace=10,
        first_connect=30,
        poll=2,
        lifetime=600,
    )
    params.update(overrides)
    return Request(**params)


def test_create_builds_image_once_then_reuses_it(manager, fake, keyfile):
    box1 = manager.create(request(keyfile))
    box2 = manager.create(request(keyfile))
    assert fake.verbs().count("build") == 1
    assert box1.name != box2.name
    assert image_tag("python:3.12-slim", "dev") in fake.images


def test_create_rebuild_flag_forces_build(manager, fake, keyfile):
    manager.create(request(keyfile))
    manager.create(request(keyfile, rebuild=True))
    assert fake.verbs().count("build") == 2


def test_create_runs_container_with_port_and_returns_connection_details(manager, fake, keyfile):
    box = manager.create(request(keyfile, port=None))
    assert box.port == 49153 and box.host == "127.0.0.1" and box.user == "dev"
    assert box.private_key is None  # caller brought their own key
    assert box.name in fake.containers
    run = fake.containers[box.name]["args"]
    assert "--detach" in run and "ephemeron=1" in run


def test_create_checks_docker_first(manager, fake, keyfile):
    fake.daemon_down = True
    with pytest.raises(Exception, match="Start Docker Desktop"):
        manager.create(request(keyfile))
    assert fake.verbs() == ["version"]


def test_create_with_bad_key_file_touches_no_docker_state(manager, fake, tmp_path):
    bad = tmp_path / "bad.pub"
    bad.write_text("not a key\n")
    with pytest.raises(Exception, match="bad.pub:1"):
        manager.create(request(bad))
    assert not fake.containers


def test_failed_start_removes_the_container(manager, fake, keyfile):
    fake.running_after_run = False
    with pytest.raises(BoxError, match="exited during start-up") as info:
        manager.create(request(keyfile))
    assert "hostkeys" in str(info.value)  # container logs are included
    assert fake.containers == {}


def test_sshd_never_answering_times_out_and_cleans_up(fake, clock, tmp_path, keyfile):
    manager = Manager(
        Docker(fake),
        root=tmp_path / "s",
        say=lambda _m: None,
        clock=clock.time,
        sleep=clock.sleep,
        probe=lambda host, port: False,
        ready_timeout=3,
    )
    with pytest.raises(BoxError, match="did not answer"):
        manager.create(request(keyfile))
    assert fake.containers == {}


def test_build_failure_is_reported_with_context(manager, fake, keyfile):
    from ephemeron.docker import CommandResult

    fake.fail["build"] = CommandResult(1, "", "manifest unknown")
    with pytest.raises(BoxError, match="python:3.12-slim.*manifest unknown"):
        manager.create(request(keyfile))
    assert fake.containers == {}


def test_copy_in_uploads_and_fixes_ownership(manager, fake, keyfile, tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("echo hi\n")
    box = manager.create(request(keyfile, copies=[(str(script), None)]))
    assert fake.containers[box.name]["copied"] == [[str(script), f"{box.name}:/home/dev/job.sh"]]
    chown = ["exec", "--user", "root", box.name, "chown", "-R", "dev", "/home/dev/job.sh"]
    assert chown in fake.calls


def test_copy_missing_source_aborts_and_cleans_up(manager, fake, keyfile, tmp_path):
    with pytest.raises(BoxError, match="does not exist"):
        manager.create(request(keyfile, copies=[(str(tmp_path / "nope"), "/tmp/nope")]))
    assert fake.containers == {}


def test_generated_keypair_lives_in_state_dir_and_dies_with_the_box(manager, fake, tmp_path):
    import shutil

    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen not installed")
    box = manager.create(Request("python:3.12-slim", "python", grace=1, first_connect=5, poll=1))
    assert box.private_key is not None and box.private_key.is_file()
    assert box.private_key.parent == tmp_path / "state" / box.name
    manager.destroy(box.name)
    assert not box.private_key.parent.exists()


def test_watch_destroys_after_last_session_leaves(manager, fake, keyfile, messages):
    fake.session_counts = [0, 1, 2, 1, 0]  # last value repeats
    box = manager.create(request(keyfile))
    reason = manager.watch(box)
    assert reason == "the last session disconnected"
    assert "session connected (1 active)" in messages
    assert "session connected (2 active)" in messages
    assert "session closed (0 active)" in messages


def test_watch_gives_up_when_nobody_connects(manager, fake, keyfile):
    fake.session_counts = [0]
    box = manager.create(request(keyfile, first_connect=20))
    assert manager.watch(box) == "nobody connected in time"


def test_watch_enforces_hard_lifetime_even_with_a_session_open(manager, fake, keyfile):
    fake.session_counts = [1]
    box = manager.create(request(keyfile, lifetime=60))
    assert manager.watch(box) == "the time limit was reached"


def test_watch_notices_container_death(manager, fake, keyfile):
    box = manager.create(request(keyfile))
    fake.containers[box.name]["running"] = False
    assert manager.watch(box) == "the container stopped on its own"


def test_watch_tolerates_a_few_exec_failures_then_gives_up(manager, fake, keyfile):
    from ephemeron.docker import CommandResult

    box = manager.create(request(keyfile))
    fake.fail["exec"] = CommandResult(1, "", "exec failed")
    with pytest.raises(BoxError, match="lost contact"):
        manager.watch(box)
    assert sum(1 for c in fake.calls if c[0] == "exec") == 5


def test_destroy_is_idempotent(manager, fake, keyfile):
    box = manager.create(request(keyfile))
    assert manager.destroy(box.name) is True
    assert manager.destroy(box.name) is False


def test_ssh_command_with_throwaway_key(tmp_path):
    box = Box("n", "127.0.0.1", 2200, "dev", tmp_path / "id", Request("x", "x"), 0)
    cmd = ssh_command(box)
    assert cmd[0] == "ssh" and "-i" in cmd and "IdentitiesOnly=yes" in cmd
    assert cmd[cmd.index("-p") + 1] == "2200"
    assert cmd[-1] == "dev@127.0.0.1"
    assert any(part.startswith("UserKnownHostsFile=") for part in cmd)


def test_ssh_command_without_key_omits_identity():
    box = Box("n", "0.0.0.0", 2200, "root", None, Request("x", "x", bind="0.0.0.0"), 0)
    cmd = ssh_command(box)
    assert "-i" not in cmd and cmd[-1] == "root@127.0.0.1"


def test_probe_host():
    assert probe_host("0.0.0.0") == "127.0.0.1"
    assert probe_host("192.168.1.5") == "192.168.1.5"


def test_names_are_unique_and_well_formed():
    names = {new_name() for _ in range(50)}
    assert len(names) == 50 and all(n.startswith("ephemeron-") and len(n) == 16 for n in names)


def test_state_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("EPHEMERON_HOME", str(tmp_path / "h"))
    assert state_root() == tmp_path / "h"


def serve_once(payload: bytes):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def accept():
        conn, _ = server.accept()
        conn.sendall(payload)
        conn.close()
        server.close()

    threading.Thread(target=accept, daemon=True).start()
    return server.getsockname()[1]


def test_ssh_banner_detects_a_real_ssh_greeting():
    port = serve_once(b"SSH-2.0-OpenSSH_9.6\r\n")
    assert ssh_banner("127.0.0.1", port) is True


def test_ssh_banner_rejects_other_protocols():
    port = serve_once(b"HTTP/1.1 400\r\n")
    assert ssh_banner("127.0.0.1", port) is False


def test_ssh_banner_false_when_nothing_listens():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert ssh_banner("127.0.0.1", port, timeout=0.5) is False
