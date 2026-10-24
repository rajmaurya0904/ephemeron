# ephemeron

**One command starts a throwaway container with the toolchain you pick and a temporary SSH endpoint, then destroys it when the last session disconnects. For testing scripts or handing a contractor short-lived access.**

You want to run an untrusted script, or let someone poke at a Python 3.11 box for an hour, and you do not want to hand out your machine or remember to `docker rm` afterwards. ephemeron builds a small image with sshd on top of the toolchain image, publishes SSH on a loopback port, installs the public key you name (or a throwaway one), counts live SSH sessions, and removes the container when nobody is left, or when the hard time limit hits.

This machine has no Docker installed, so the run below uses the stand-in `docker` executable from the test-suite (`tests/fake_docker.py`); the CLI, the readiness probe and the session-polling loop are the real code. A second block shows what happens with no Docker at all.

```console
$ ephemeron --docker-bin ./fakedocker.cmd run python -k contractor.pub --poll 1s --grace 2s
docker 27.0.0 reachable
building an SSH-enabled python:3.12-slim image (cached after this run)...
ephemeron-ba0ce2 is up (python:3.12-slim, user dev)
  connect:  ssh -p 65475 -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o LogLevel=ERROR dev@127.0.0.1
  ends:     2s after the last session disconnects, or after 1h at the latest
  waiting for the first connection (gives up after 10m)
session connected (1 active)
session closed (0 active)
destroying in 2s unless someone reconnects
ephemeron-ba0ce2 destroyed (the last session disconnected)
$ echo $?
0
```

```console
$ ephemeron run node:20 --ttl 1m
ephemeron: The 'docker' executable was not found on PATH. Install Docker (Docker Desktop on Windows/macOS) or pass --docker-bin.
$ echo $?
1
```

## Install

ephemeron is not on PyPI. It needs Python 3.11+, a working `docker` CLI, and an `ssh` client only if you use `--connect`. There are no Python dependencies.

```bash
pip install -e .
```

## Usage

```bash
# Python box, login with your own key; prints the ssh command to use
ephemeron run python -k ~/.ssh/id_ed25519.pub

# Pinned toolchain, hard 30 minute limit, no throwaway-key hassle: open ssh for me and clean up after
ephemeron run node:20 --ttl 30m --connect

# Any image, with files copied in and resource limits
ephemeron run --image ubuntu:24.04 --copy ./script.sh --memory 512m --cpus 1

# See what the command would do without starting anything (prints the Dockerfile and docker run line)
ephemeron run go --dry-run

# Housekeeping
ephemeron toolchains      # built-in toolchains
ephemeron list            # boxes and when they expire
ephemeron destroy NAME    # kill one now
ephemeron prune           # remove expired boxes and stale key material
```

Durations take values like `10s`, `5m`, `1h`. With no `-k`, a fresh keypair is generated for the box (needs `ssh-keygen`). `--json` prints connection details for scripts.

## How it works

- Builds `ephemeron/<toolchain>-<hash>` from the base image, adding openssh-server and a login user via apt, apk, dnf or yum. The image is cached; `--rebuild` forces a fresh build.
- Starts the container detached with a loopback-published port, key-only auth (password login disabled), memory and pid limits, and labels recording the expiry time.
- Waits until a real TCP connection to the port receives an SSH banner before announcing the box.
- Polls the container's process list (`docker exec`) every `--poll` interval and counts `sshd: <user> [priv]` processes, so half-open or unauthenticated connections do not count as sessions.
- A small state machine decides when to destroy: `--first-connect` if nobody ever arrives, `--grace` after the last session closes (a reconnect cancels it), and `--ttl` as a hard cap regardless.
- Ctrl-C or SIGTERM destroys the box; `prune` removes boxes past their expiry label (or all of them with `--all`).

## Limitations

- Needs Docker. It was developed and tested against a stand-in docker executable here; the Dockerfile and `docker run` line are checked by tests and `--dry-run`, but I have not run it against a real Docker engine or connected a real ssh client.
- Session counting depends on `ps` output inside the container, so images without a usable `ps` (or very minimal ones) may never see a session; `--first-connect` and `--ttl` still bound the lifetime.
- The default connect command disables host key checking, since the host key is new on every box. It is bound to 127.0.0.1 unless you pass `--bind`; exposing it wider is your call.
- Containers share the host kernel and are not a hardened sandbox for hostile code.
- Only apt, apk, dnf and yum based images are supported for the SSH layer.
- The `ephemeron run` process must stay alive to enforce the limits; if it is killed hard, the box stays until `ephemeron prune` (after expiry) or `ephemeron destroy NAME`.

## Development

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
```

## Licence

MIT, see [LICENSE](LICENSE).
