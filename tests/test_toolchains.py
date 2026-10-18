import pytest

from ephemeron.toolchains import TOOLCHAINS, ToolchainError, resolve_toolchain, validate_image


@pytest.mark.parametrize(
    ("spec", "image"),
    [
        ("python", "python:3.12-slim"),
        ("python:3.11", "python:3.11-slim"),
        ("node", "node:22-bookworm-slim"),
        ("node:20", "node:20-bookworm-slim"),
        ("GO:1.22", "golang:1.22-bookworm"),
        ("ubuntu:22.04", "ubuntu:22.04"),
        ("alpine", "alpine:3.20"),
    ],
)
def test_resolve_known_toolchains(spec, image):
    assert resolve_toolchain(spec) == image


def test_unknown_toolchain_lists_choices():
    with pytest.raises(ToolchainError) as info:
        resolve_toolchain("cobol")
    message = str(info.value)
    assert "cobol" in message and "python" in message and "--image" in message


@pytest.mark.parametrize("version", ["a b", "-x", "1;rm", "x" * 80])
def test_bad_versions_rejected(version):
    with pytest.raises(ToolchainError):
        resolve_toolchain(f"python:{version}")


def test_every_builtin_resolves_to_a_valid_image():
    for name in TOOLCHAINS:
        assert validate_image(resolve_toolchain(name))


@pytest.mark.parametrize(
    "image",
    ["ubuntu", "ghcr.io/org/app:1.2", "localhost:5000/team/img:latest", "a/b@sha256:" + "0" * 64],
)
def test_valid_images(image):
    assert validate_image(image) == image


@pytest.mark.parametrize("image", ["", "-v /:/host", "bad image", "--privileged", "x;y"])
def test_invalid_images(image):
    with pytest.raises(ToolchainError):
        validate_image(image)
