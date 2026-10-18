"""SSH key handling: parse authorized public keys, or mint a throwaway keypair."""

from __future__ import annotations

import base64
import binascii
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class KeyFileError(ValueError):
    """A key file is unreadable or not an OpenSSH public key."""


KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)


def validate_public_key(line: str) -> str:
    """Return the normalised ``type blob [comment]`` line or raise :class:`KeyFileError`."""
    parts = line.strip().split(None, 2)
    if len(parts) < 2:
        raise KeyFileError("not an OpenSSH public key (expected 'ssh-ed25519 AAAA... comment')")
    key_type, blob = parts[0], parts[1]
    if key_type not in KEY_TYPES:
        raise KeyFileError(f"unsupported key type '{key_type}'")
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyFileError("public key data is not valid base64") from exc
    if not raw.startswith(len(key_type).to_bytes(4, "big") + key_type.encode()):
        raise KeyFileError("public key data does not match its declared type")
    return " ".join(parts)


def read_public_keys(path: Path) -> list[str]:
    """Read every key from an authorized_keys-style or single ``.pub`` file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KeyFileError(f"cannot read {path}: {exc.strerror or exc}") from exc
    keys = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            keys.append(validate_public_key(line))
        except KeyFileError as exc:
            raise KeyFileError(f"{path}:{number}: {exc}") from exc
    if not keys:
        raise KeyFileError(f"{path} contains no public keys")
    return keys


@dataclass(frozen=True)
class Keypair:
    private_path: Path
    public_key: str


def generate_keypair(directory: Path, comment: str) -> Keypair:
    """Create an ed25519 keypair in ``directory`` using the system ``ssh-keygen``."""
    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        raise KeyFileError(
            "ssh-keygen was not found on PATH. Install the OpenSSH client, or pass "
            "--authorized-key with an existing public key."
        )
    directory.mkdir(parents=True, exist_ok=True)
    private = directory / "id_ed25519"
    result = subprocess.run(
        [keygen, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(private)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise KeyFileError(f"ssh-keygen failed: {(result.stderr or result.stdout).strip()}")
    public = validate_public_key((directory / "id_ed25519.pub").read_text(encoding="utf-8"))
    return Keypair(private, public)
