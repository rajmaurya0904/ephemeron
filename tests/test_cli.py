import argparse
import json
import os

import pytest

from ephemeron import __version__, cli
from ephemeron.box import Manager
from ephemeron.docker import Docker
from ephemeron.keys import Keypair
from tests.fakes import FakeClock, FakeDocker, ed25519_line


@pytest.fixture
def env(tmp_path):
    clock = FakeClock()
    fake = FakeDocker(clock)
    messages: list[str] = []
    manager = Manager(
        Docker(fake),
        root=tmp_path / "state",
        say=messages.append,
        clock=clock.time,
        sleep=clock.sleep,
        probe=lambda host, port: True,
        ready_timeout=5,
    )
    key = tmp_path / "k.pub"
    key.write_text(ed25519_line("c") + "\n")
    return fake, manager, messages, key


def stub_keygen(monkeypatch, directory):
    """--connect needs the throwaway keypair; avoid shelling out to ssh-keygen."""
    private = directory / "id"
    monkeypatch.setattr(
        "ephemeron.box.generate_keypair", lambda d, c: Keypair(private, ed25519_line("gen"))
    )
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    return private


def run_main(env, *argv, remote=()):
    fake, manager, messages, key = env
    return cli.main([*argv, *(["--", *remote] if remote else [])], manager=manager)


# -- durations ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90", 90), ("90s", 90), ("15m", 900), ("2h", 7200), ("1d", 86400), ("1.5h", 5400)],
)
def test_parse_duration(text, seconds):
    assert cli.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "m", "-5", "10x", "1h30m"])
def test_parse_duration_rejects(text):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_duration(text)


def test_seconds_formatting():
    assert cli._seconds(10) == "10s"
    assert cli._seconds(3600) == "1h"
    assert cli._seconds(90) == "90s"
    assert cli._seconds(86400) == "1d"


# -- parsing -----------------------------------------------------------------------


def test_split_remote_command():
    assert cli.split_remote_command(["run", "python", "--", "ls", "-l"]) == (
        ["run", "python"],
        ["ls", "-l"],
    )
    assert cli.split_remote_command(["run", "python"]) == (["run", "python"], [])


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_toolchains_listing(capsys):
    assert cli.main(["toolchains"]) == 0
    out = capsys.readouterr().out
    assert "python:3.12-slim" in out and "node:22-bookworm-slim" in out


def test_run_needs_toolchain_or_image(env, capsys):
    assert run_main(env, "run") == 2
    assert "exactly one" in capsys.readouterr().err


def test_run_rejects_both_toolchain_and_image(env, capsys):
    assert run_main(env, "run", "python", "--image", "ubuntu") == 2


def test_unknown_toolchain_exits_2(env, capsys):
    assert run_main(env, "run", "cobol") == 2
    assert "Unknown toolchain" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flags",
    [["--user", "Bad User"], ["--port", "70000"], ["--env", "oops"]],
)
def test_bad_options_exit_2(env, flags, capsys):
    assert run_main(env, "run", "python", *flags) == 2
    assert capsys.readouterr().err.startswith("ephemeron:")


def test_bad_key_file_exits_2(env, tmp_path, capsys):
    missing = tmp_path / "missing.pub"
    assert run_main(env, "run", "python", "-k", str(missing)) == 2
    assert "cannot read" in capsys.readouterr().err


def test_remote_command_requires_connect(env, capsys):
    fake, _, _, key = env
    assert run_main(env, "run", "python", "-k", str(key), remote=["ls"]) == 2
    assert not fake.containers


# -- dry run -----------------------------------------------------------------------


def test_dry_run_prints_plan_without_touching_docker(env):
    fake, _, messages, _ = env
    assert run_main(env, "run", "node:20", "--dry-run", "--ttl", "30m", "--memory", "512m") == 0
    text = "\n".join(messages)
    assert "FROM node:20-bookworm-slim" in text
    assert "docker run --detach" in text and "--memory 512m" in text
    assert "ephemeron.expires=1800" in text
    assert fake.calls == []


# -- run ---------------------------------------------------------------------------


def test_run_full_lifecycle(env):
    fake, _, messages, key = env
    fake.session_counts = [0, 1, 1, 0]
    code = run_main(env, "run", "python", "-k", str(key), "--poll", "2s", "--grace", "4s")
    assert code == 0
    assert fake.containers == {}  # destroyed
    joined = "\n".join(messages)
    assert "is up (python:3.12-slim, user dev)" in joined
    assert "connect:  ssh" in joined and "dev@127.0.0.1" in joined
    assert "session connected (1 active)" in joined
    assert "destroyed (the last session disconnected)" in messages[-1]


def test_run_json_output_is_machine_readable(env):
    fake, _, messages, key = env
    fake.session_counts = [1, 0]
    run_main(env, "run", "go", "-k", str(key), "--json", "--grace", "1s", "--poll", "1s")
    details = json.loads(next(m for m in messages if m.startswith("{")))
    assert details["port"] == 49153 and details["user"] == "dev"
    assert details["ssh"][0] == "ssh" and details["ssh"][-1] == "dev@127.0.0.1"


def test_run_public_bind_warns(env):
    fake, _, messages, key = env
    fake.session_counts = [1, 0]
    run_main(env, "run", "python", "-k", str(key), "--bind", "0.0.0.0", "--grace", "1s")
    assert any("anyone with the key" in m for m in messages)


def test_run_gives_up_when_nobody_connects(env):
    fake, _, messages, key = env
    fake.session_counts = [0]
    assert run_main(env, "run", "python", "-k", str(key), "--first-connect", "10s") == 0
    assert messages[-1].endswith("destroyed (nobody connected in time)")
    assert fake.containers == {}


def test_run_interrupt_destroys_box_and_exits_130(env, monkeypatch):
    fake, manager, messages, key = env

    def interrupted(box):
        raise KeyboardInterrupt

    monkeypatch.setattr(manager, "watch", interrupted)
    assert run_main(env, "run", "python", "-k", str(key)) == 130
    assert fake.containers == {}
    assert messages[-1].endswith("destroyed (interrupted)")


def test_run_docker_down_exits_1(env, capsys):
    fake, _, _, key = env
    fake.daemon_down = True
    assert run_main(env, "run", "python", "-k", str(key)) == 1
    assert "Start Docker Desktop" in capsys.readouterr().err


def test_connect_runs_ssh_with_remote_command_then_destroys(env, monkeypatch):
    fake, _, messages, key = env
    seen = {}

    def fake_call(command):
        seen["command"] = command
        assert len(fake.containers) == 1  # box is alive while ssh runs
        return 7

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/ssh")
    private = stub_keygen(monkeypatch, key.parent)
    code = run_main(env, "run", "python", "--connect", remote=["python", "job.py"])
    assert code == 7  # ssh's own exit status is passed through
    assert seen["command"][-2:] == ["python", "job.py"]
    assert seen["command"][0] == "ssh" and str(private) in seen["command"]
    assert fake.containers == {}
    assert messages[-1].endswith("destroyed (the ssh session ended)")


def test_connect_requires_ssh_client(env, monkeypatch, capsys):
    fake, _, _, key = env
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    stub_keygen(monkeypatch, key.parent)
    assert run_main(env, "run", "python", "--connect") == 1
    assert "needs the 'ssh' client" in capsys.readouterr().err
    assert fake.containers == {}


# -- list / destroy / prune ----------------------------------------------------------


def make_box(env, extra=()):
    fake, _, _, key = env
    fake.session_counts = [1]
    from ephemeron.box import Request

    _, manager, _, _ = env
    return manager.create(Request("python:3.12-slim", "python", key_files=[key], **dict(extra)))


def test_list_empty(env):
    _, _, messages, _ = env
    assert run_main(env, "list") == 0
    assert messages == ["no ephemeron boxes"]


def test_list_shows_expiry(env):
    box = make_box(env, {"lifetime": 7200})
    _, _, messages, _ = env
    assert run_main(env, "list") == 0
    row = next(m for m in messages if m.startswith(box.name))
    assert "python" in row and "running" in row and "in 2h" in row


def test_destroy_named_box(env):
    box = make_box(env)
    fake, _, messages, _ = env
    assert run_main(env, "destroy", box.name) == 0
    assert box.name not in fake.containers and f"{box.name} destroyed" in messages


def test_destroy_unknown_and_malformed_names(env):
    _, _, messages, _ = env
    assert run_main(env, "destroy", "ephemeron-aaaaaa") == 1
    assert run_main(env, "destroy", "some-other-container") == 1
    assert "some-other-container: not an ephemeron box name" in messages


def test_destroy_refuses_to_touch_non_ephemeron_names(env):
    fake, _, _, _ = env
    run_main(env, "destroy", "production-db")
    assert not any(call[0] == "rm" for call in fake.calls)


def test_prune_removes_only_expired(env):
    young = make_box(env, {"lifetime": 7200})
    old = make_box(env, {"lifetime": 60})
    fake, manager, messages, _ = env
    later = fake.clock.now + 120  # past the short box's expiry, well before the long one's
    manager.clock = lambda: later
    assert run_main(env, "prune") == 0
    assert old.name not in fake.containers and young.name in fake.containers
    assert f"destroyed {old.name}" in messages


def test_prune_all(env):
    make_box(env)
    make_box(env)
    fake, _, _, _ = env
    assert run_main(env, "prune", "--all") == 0
    assert fake.containers == {}


def test_prune_nothing(env):
    _, _, messages, _ = env
    run_main(env, "prune")
    assert "nothing to prune" in messages
