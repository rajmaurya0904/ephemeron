"""Named toolchains and how they map to container images."""

from __future__ import annotations

import re
from dataclasses import dataclass


class ToolchainError(ValueError):
    """The requested toolchain or image is not usable."""


@dataclass(frozen=True)
class Toolchain:
    name: str
    image: str  # contains {v} where the version goes
    default_version: str
    description: str

    def resolve(self, version: str | None = None) -> str:
        return self.image.format(v=version or self.default_version)


TOOLCHAINS: dict[str, Toolchain] = {
    t.name: t
    for t in [
        Toolchain("python", "python:{v}-slim", "3.12", "CPython with pip"),
        Toolchain("node", "node:{v}-bookworm-slim", "22", "Node.js with npm"),
        Toolchain("go", "golang:{v}-bookworm", "1.23", "Go compiler and tools"),
        Toolchain("rust", "rust:{v}-slim-bookworm", "1", "rustc and cargo"),
        Toolchain("ruby", "ruby:{v}-slim", "3.3", "Ruby with gem and bundler"),
        Toolchain("java", "eclipse-temurin:{v}-jdk", "21", "Temurin JDK"),
        Toolchain("php", "php:{v}-cli", "8.3", "PHP command line"),
        Toolchain("ubuntu", "ubuntu:{v}", "24.04", "Bare Ubuntu"),
        Toolchain("debian", "debian:{v}-slim", "12", "Bare Debian"),
        Toolchain("alpine", "alpine:{v}", "3.20", "Bare Alpine"),
    ]
}

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IMAGE_RE = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(@sha256:[a-f0-9]{64})?$"
)


def resolve_toolchain(spec: str) -> str:
    """Turn ``python``, ``node:20`` and the like into a concrete image reference."""
    name, _, version = spec.partition(":")
    toolchain = TOOLCHAINS.get(name.lower())
    if toolchain is None:
        known = ", ".join(sorted(TOOLCHAINS))
        raise ToolchainError(
            f"Unknown toolchain '{name}'. Choose one of: {known} (optionally name:version), "
            "or pass --image for any other image."
        )
    if version and not _VERSION_RE.match(version):
        raise ToolchainError(f"'{version}' is not a valid version tag")
    return toolchain.resolve(version or None)


def validate_image(image: str) -> str:
    if not _IMAGE_RE.match(image) or len(image) > 255:
        raise ToolchainError(
            f"'{image}' does not look like a docker image reference (e.g. registry/name:tag)"
        )
    return image
