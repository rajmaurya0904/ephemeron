import sys
import textwrap

import pytest

from ephemeron.docker import (
    CommandResult,
    Docker,
    DockerCLI,
    DockerError,
    DockerUnavailable,
    parse_published_port,
)
from tests.fakes import FakeDocker


@pytest.fixture
def fake() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def docker(fake) -> Docker:
    return Docker(fake)


def test_parse_published_port_prefers_ipv4():
    assert parse_published_port("[::]:49153\n127.0.0.1:49153\n") == ("127.0.0.1", 49153)


def test_parse_published_port_ipv6_only():
    assert parse_published_port("[::]:50000\n") == ("[::]", 50000)


def test_parse_published_port_garbage():
    with pytest.raises(DockerError, match="published SSH port"):
        parse_published_port("nothing here")


def test_check_returns_version(docker):
    assert docker.check() == "27.3.1"


def test_daemon_down_is_unavailable(fake, docker):
    fake.daemon_down = True
    with pytest.raises(DockerUnavailable, match="Start Docker Desktop"):
        docker.check()


def test_image_exists(fake, docker):
    assert not docker.image_exists("a:1")
    fake.images.add("a:1")
    assert docker.image_exists("a:1")


def test_image_exists_reports_daemon_down(fake, docker):
    fake.daemon_down = True
    with pytest.raises(DockerUnavailable):
        docker.image_exists("a:1")


def test_build_sends_dockerfile_on_stdin(fake, docker):
    docker.build("t:1", "FROM x\n")
    assert fake.calls[0] == ["build", "--tag", "t:1", "-"]
    assert fake.inputs[0] == "FROM x\n"


def test_build_failure_message(fake, docker):
    fake.fail["build"] = CommandResult(1, "", "pull access denied")
    with pytest.raises(DockerError, match="pull access denied"):
        docker.build("t:1", "FROM x\n")


def test_remove_missing_container_is_not_an_error(docker):
    assert docker.remove("ghost") is False


def test_remove_existing(fake, docker):
    fake.containers["c"] = {"running": True, "labels": {}, "publish": "", "copied": []}
    assert docker.remove("c") is True
    assert "c" not in fake.containers


def test_remove_surfaces_other_errors(fake, docker):
    fake.fail["rm"] = CommandResult(1, "", "permission denied")
    with pytest.raises(DockerError, match="permission denied"):
        docker.remove("c")


def test_is_running_false_for_unknown(docker):
    assert docker.is_running("ghost") is False


def test_exec_with_user(fake, docker):
    fake.containers["c"] = {"running": True, "labels": {}, "publish": "", "copied": []}
    docker.exec("c", ["chown", "-R", "dev", "/x"], user="root")
    assert fake.calls[0][:3] == ["exec", "--user", "root"]


def test_missing_binary_is_unavailable():
    with pytest.raises(DockerUnavailable, match="not found on PATH"):
        DockerCLI("definitely-not-docker").run(["ps"])


def test_real_subprocess_round_trip_with_stdin(tmp_path):
    script = tmp_path / "fake_docker.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            data = sys.stdin.read()
            print(len(data), sys.argv[1:])
            sys.exit(0 if data else 4)
            """
        )
    )
    launcher = tmp_path / ("d.cmd" if sys.platform == "win32" else "d")
    if sys.platform == "win32":
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
    cli = DockerCLI(str(launcher))
    result = cli.run(["build", "-"], input_text="FROM x")
    assert result.returncode == 0 and result.stdout.startswith("6 ")
    assert cli.run(["ps"]).returncode == 4  # no stdin attached: empty input
