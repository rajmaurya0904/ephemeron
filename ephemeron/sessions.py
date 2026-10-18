"""Counting live SSH sessions inside a container, and deciding when the box should die."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Listing every process command line needs nothing but a POSIX shell, so it works in
# slim and Alpine images alike. NUL separators in /proc/<pid>/cmdline become spaces.
LIST_PROCESSES = (
    "for f in /proc/[0-9]*/cmdline; do tr '\\000' ' ' < \"$f\" 2>/dev/null; echo; done"
)

# After authentication OpenSSH keeps a privileged monitor process per login, titled
# "sshd: USER [priv]" (OpenSSH 9.8 and newer: "sshd-session: USER [priv]"). Connections
# that have not authenticated yet are titled "sshd: [accepted]" or "sshd: unknown [priv]"
# and must not count, otherwise any port scan would keep a box alive.
_SESSION_RE = re.compile(r"^sshd(?:-session)?: (?P<user>[^\s\[@]+) \[priv\]\s*$")


def count_sessions(process_listing: str) -> int:
    """Number of authenticated SSH logins in the output of :data:`LIST_PROCESSES`."""
    total = 0
    for line in process_listing.splitlines():
        match = _SESSION_RE.match(line.strip())
        if match and match.group("user") != "unknown":
            total += 1
    return total


@dataclass(frozen=True)
class Decision:
    destroy: bool
    reason: str = ""
    event: str = ""


class IdleTracker:
    """Pure state machine fed with ``(active_sessions, now)`` samples.

    A box is destroyed when any of these happens:

    * ``lifetime`` seconds have passed since creation (hard ceiling);
    * nobody connected within ``first_connect`` seconds;
    * at least one session was seen, and none have been open for ``grace`` seconds.
    """

    def __init__(self, started: float, lifetime: float, first_connect: float, grace: float):
        self.started = started
        self.lifetime = lifetime
        self.first_connect = first_connect
        self.grace = grace
        self.ever_connected = False
        self.last_count = 0
        self.idle_since: float | None = None

    def update(self, active: int, now: float) -> Decision:
        event = ""
        if active != self.last_count:
            if active > self.last_count:
                event = f"session connected ({active} active)"
            else:
                event = f"session closed ({active} active)"
        self.last_count = active

        if now - self.started >= self.lifetime:
            return Decision(True, "the time limit was reached", event)
        if active > 0:
            self.ever_connected = True
            self.idle_since = None
            return Decision(False, "", event)
        if not self.ever_connected:
            if now - self.started >= self.first_connect:
                return Decision(True, "nobody connected in time", event)
            return Decision(False, "", event)
        if self.idle_since is None:
            self.idle_since = now
        if now - self.idle_since >= self.grace:
            return Decision(True, "the last session disconnected", event)
        return Decision(False, "", event)

    def seconds_until_destroy(self, now: float) -> float | None:
        """Countdown to a pending idle-destroy, or None when one is not pending."""
        if self.idle_since is None:
            return None
        return max(0.0, self.grace - (now - self.idle_since))
