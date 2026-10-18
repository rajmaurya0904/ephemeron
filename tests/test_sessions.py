import pytest

from ephemeron.sessions import IdleTracker, count_sessions
from tests.fakes import process_listing


def test_no_sessions():
    assert count_sessions(process_listing(0)) == 0


def test_counts_one_per_login_not_per_process():
    assert count_sessions(process_listing(1)) == 1
    assert count_sessions(process_listing(3)) == 3


def test_unauthenticated_connections_do_not_count():
    assert count_sessions(process_listing(0, preauth=5)) == 0
    assert count_sessions("sshd: unknown [priv]\nsshd: [accepted]\n") == 0


def test_openssh_98_process_names():
    assert count_sessions("sshd-session: dev [priv]\nsshd-session: dev@notty\n") == 1


def test_trailing_nul_spaces_are_tolerated():
    assert count_sessions("sshd: dev [priv] \n\n") == 1


def test_other_processes_that_mention_sshd_do_not_count():
    listing = "grep sshd: dev [priv]\nbash -c sshd: dev [priv]\n"
    assert count_sessions(listing) == 0


def make(**overrides) -> IdleTracker:
    params = dict(started=0.0, lifetime=3600.0, first_connect=60.0, grace=10.0)
    params.update(overrides)
    return IdleTracker(**params)


def test_waits_quietly_for_first_connection():
    tracker = make()
    assert not tracker.update(0, 5).destroy
    assert not tracker.update(0, 59).destroy


def test_gives_up_if_nobody_ever_connects():
    decision = make().update(0, 60)
    assert decision.destroy and "nobody connected" in decision.reason


def test_destroys_after_grace_following_last_disconnect():
    tracker = make()
    assert tracker.update(1, 5).event == "session connected (1 active)"
    assert not tracker.update(1, 100).destroy
    closed = tracker.update(0, 200)
    assert closed.event == "session closed (0 active)" and not closed.destroy
    assert not tracker.update(0, 209).destroy
    final = tracker.update(0, 210)
    assert final.destroy and final.reason == "the last session disconnected"


def test_reconnect_within_grace_cancels_destruction():
    tracker = make()
    tracker.update(1, 1)
    tracker.update(0, 50)
    assert tracker.seconds_until_destroy(55) == 5
    assert not tracker.update(1, 55).destroy
    assert tracker.seconds_until_destroy(56) is None
    assert not tracker.update(0, 100).destroy  # grace restarts from 100
    assert tracker.update(0, 110).destroy


def test_stays_alive_while_any_session_remains():
    tracker = make()
    tracker.update(2, 1)
    assert not tracker.update(1, 500).destroy
    assert tracker.update(1, 500).event == ""


def test_hard_lifetime_wins_even_with_active_sessions():
    tracker = make(lifetime=100)
    tracker.update(1, 10)
    decision = tracker.update(1, 100)
    assert decision.destroy and "time limit" in decision.reason


@pytest.mark.parametrize("grace", [0, 1, 30])
def test_grace_is_respected(grace):
    tracker = make(grace=grace)
    tracker.update(1, 1)
    assert tracker.update(0, 2).destroy == (grace == 0)
    assert tracker.update(0, 2 + grace).destroy
