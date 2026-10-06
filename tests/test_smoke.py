"""Smoke test: package imports cleanly. Replace/extend as modules land."""

import ephemeron


def test_version_is_set() -> None:
    assert ephemeron.__version__
