import base64
import shutil
import struct

import pytest

from ephemeron.keys import KeyFileError, generate_keypair, read_public_keys, validate_public_key


def ed25519_line(comment: str = "alice@laptop") -> str:
    kind = b"ssh-ed25519"
    blob = struct.pack(">I", len(kind)) + kind + struct.pack(">I", 32) + bytes(range(32))
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".strip()


def test_valid_key_round_trips():
    line = ed25519_line()
    assert validate_public_key(line + "\n") == line


def test_comment_is_optional():
    line = ed25519_line("")
    assert validate_public_key(line) == line


def test_unsupported_type():
    with pytest.raises(KeyFileError, match="unsupported key type"):
        validate_public_key("ssh-dss AAAA x")


def test_garbage_base64():
    with pytest.raises(KeyFileError, match="base64"):
        validate_public_key("ssh-ed25519 not*base64 x")


def test_blob_must_match_declared_type():
    blob = base64.b64encode(struct.pack(">I", 7) + b"ssh-rsa" + b"\x00" * 8).decode()
    with pytest.raises(KeyFileError, match="does not match"):
        validate_public_key(f"ssh-ed25519 {blob}")


def test_single_token_rejected():
    with pytest.raises(KeyFileError, match="not an OpenSSH public key"):
        validate_public_key("ssh-ed25519")


def test_read_public_keys_skips_comments_and_blanks(tmp_path):
    path = tmp_path / "authorized_keys"
    path.write_text(f"# contractors\n\n{ed25519_line('a')}\n{ed25519_line('b')}\n")
    keys = read_public_keys(path)
    assert [k.split()[-1] for k in keys] == ["a", "b"]


def test_read_public_keys_reports_line_numbers(tmp_path):
    path = tmp_path / "keys.pub"
    path.write_text(f"{ed25519_line()}\nnonsense here\n")
    with pytest.raises(KeyFileError, match=r"keys\.pub:2"):
        read_public_keys(path)


def test_read_public_keys_empty_file(tmp_path):
    path = tmp_path / "empty.pub"
    path.write_text("# nothing\n")
    with pytest.raises(KeyFileError, match="no public keys"):
        read_public_keys(path)


def test_read_public_keys_missing_file(tmp_path):
    with pytest.raises(KeyFileError, match="cannot read"):
        read_public_keys(tmp_path / "nope.pub")


def test_generate_keypair_without_ssh_keygen(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(KeyFileError, match="ssh-keygen was not found"):
        generate_keypair(tmp_path / "k", "c")


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen not installed")
def test_generate_keypair_makes_a_usable_pair(tmp_path):
    pair = generate_keypair(tmp_path / "box", "ephemeron-test")
    assert pair.private_path.is_file()
    assert pair.public_key.startswith("ssh-ed25519 ")
    assert pair.public_key.endswith("ephemeron-test")
    assert validate_public_key(pair.public_key) == pair.public_key
