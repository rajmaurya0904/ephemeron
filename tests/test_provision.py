import shutil
import subprocess

import pytest

from ephemeron.provision import (
    KEYS_ENV,
    BoxSpec,
    describe_command,
    home_for,
    image_tag,
    parse_env,
    render_dockerfile,
    run_args,
    startup_script,
)


def spec(**overrides) -> BoxSpec:
    base = dict(
        name="ephemeron-abc123",
        image="ephemeron/python:1",
        toolchain="python",
        authorized_keys=["ssh-ed25519 AAAA one", "ssh-ed25519 BBBB two"],
    )
    base.update(overrides)
    return BoxSpec(**base)


def opts(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def test_dockerfile_starts_from_base_and_covers_all_package_managers():
    text = render_dockerfile("python:3.12-slim")
    assert text.startswith("FROM python:3.12-slim\n")
    for manager in ("apt-get", "apk", "dnf", "yum"):
        assert manager in text
    assert "openssh-server" in text and "EXPOSE 22" in text


def test_dockerfile_creates_unlocked_login_user():
    text = render_dockerfile("alpine:3.20", "dev")
    assert "useradd --create-home --shell /bin/bash dev" in text
    assert "adduser -D -s /bin/bash dev" in text
    assert "echo 'dev:*' | chpasswd -e" in text


def test_dockerfile_for_root_makes_no_user():
    text = render_dockerfile("debian:12-slim", "root")
    assert "useradd" not in text and "chpasswd" not in text


def test_dockerfile_never_contains_key_material():
    assert "ssh-ed25519" not in render_dockerfile("python:3.12-slim")
    assert "ssh-keygen" not in render_dockerfile("python:3.12-slim")


def test_image_tag_is_stable_and_tracks_inputs():
    a = image_tag("python:3.12-slim", "dev")
    assert a == image_tag("python:3.12-slim", "dev")
    assert a != image_tag("python:3.12-slim", "root")
    assert a != image_tag("python:3.11-slim", "dev")
    assert a.startswith("ephemeron/python-3.12-slim:")


def test_image_tag_is_a_legal_reference_for_odd_bases():
    tag = image_tag("ghcr.io/Some_Org/App:1.0")
    repo, _, version = tag.partition(":")
    assert repo == repo.lower() and " " not in tag and len(version) == 10


def test_home_dirs():
    assert home_for("root") == "/root"
    assert home_for("dev") == "/home/dev"


def test_startup_script_generates_host_keys_at_runtime_and_installs_key():
    script = startup_script("dev")
    assert "ssh-keygen -A" in script
    assert f'"${KEYS_ENV}" > /home/dev/.ssh/authorized_keys' in script
    assert "chmod 600 /home/dev/.ssh/authorized_keys" in script
    assert script.rstrip().splitlines()[-1].startswith("exec /usr/sbin/sshd ")


def test_startup_script_hardens_sshd():
    last = startup_script("dev").splitlines()[-1]
    for option in (
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "PermitRootLogin=prohibit-password",
        "AllowUsers=dev",
    ):
        assert option in last


def test_startup_script_wipes_key_variable_before_exec():
    lines = startup_script("dev").splitlines()
    assert lines.index(f"unset {KEYS_ENV}") < len(lines) - 1


def test_startup_script_for_root():
    script = startup_script("root")
    assert "/root/.ssh/authorized_keys" in script and "AllowUsers=root" in script


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh to syntax-check with")
def test_startup_script_is_valid_shell():
    result = subprocess.run(
        ["sh", "-n", "-c", startup_script("dev")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_run_args_labels_and_expiry():
    args = run_args(spec(lifetime=600), now=1000)
    labels = opts(args, "--label")
    assert "ephemeron=1" in labels
    assert "ephemeron.expires=1600" in labels
    assert "ephemeron.toolchain=python" in labels


def test_run_args_publish_random_port_on_loopback_by_default():
    assert opts(run_args(spec()), "--publish") == ["127.0.0.1::22"]


def test_run_args_fixed_port_and_bind():
    args = run_args(spec(bind="0.0.0.0", host_port=2222))
    assert opts(args, "--publish") == ["0.0.0.0:2222:22"]


def test_run_args_pass_keys_in_environment_one_per_line():
    args = run_args(spec())
    value = next(v for v in opts(args, "--env") if v.startswith(KEYS_ENV))
    assert value.split("=", 1)[1].splitlines() == ["ssh-ed25519 AAAA one", "ssh-ed25519 BBBB two"]


def test_run_args_resource_limits_optional():
    args = run_args(spec(memory="512m", cpus="1.5", pids_limit=64))
    assert opts(args, "--memory") == ["512m"]
    assert opts(args, "--cpus") == ["1.5"]
    assert opts(args, "--pids-limit") == ["64"]
    bare = run_args(spec(memory=None, pids_limit=None))
    assert "--memory" not in bare and "--pids-limit" not in bare


def test_run_args_end_with_image_and_shell_script():
    args = run_args(spec())
    assert args[-4:-2] == ["ephemeron/python:1", "sh"]
    assert args[-2] == "-c" and "exec /usr/sbin/sshd" in args[-1]


def test_run_args_extra_env():
    args = run_args(spec(env=[("DEBUG", "1")]))
    assert "DEBUG=1" in opts(args, "--env")


def test_parse_env():
    assert parse_env(["A=1", "B=two=2", "C="]) == [("A", "1"), ("B", "two=2"), ("C", "")]


@pytest.mark.parametrize("bad", ["NOEQUALS", "1A=x", "A B=x", f"{KEYS_ENV}=x"])
def test_parse_env_rejects(bad):
    with pytest.raises(ValueError):
        parse_env([bad])


def test_describe_command_hides_keys_and_script():
    text = describe_command(run_args(spec()))
    assert "AAAA" not in text and "<elided>" in text and "<startup script>" in text
    assert text.startswith("docker run --detach --name ephemeron-abc123")
