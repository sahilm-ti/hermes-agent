"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _worker_gh_token_override(monkeypatch):
    """Set HERMES_KANBAN_WORKER_GH_TOKEN_OVERRIDE to a sentinel value.

    ``_default_spawn`` now requires ``GH_TOKEN_SAHILM_AI`` in the
    dispatcher env so spawned workers always authenticate as the bot
    account (sahilm-ai).  Test suites run without that real token, so we
    bypass the requirement via the escape-hatch env var.

    Tests that want to exercise the ``GH_TOKEN_SAHILM_AI`` requirement
    explicitly call ``monkeypatch.delenv("HERMES_KANBAN_WORKER_GH_TOKEN_OVERRIDE")``
    to override this fixture.
    """
    import os

    if "HERMES_KANBAN_WORKER_GH_TOKEN_OVERRIDE" not in os.environ:
        monkeypatch.setenv("HERMES_KANBAN_WORKER_GH_TOKEN_OVERRIDE", "ghs_test_fixture_token")


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    monkeypatch.setattr(
        _cli_main, "_detect_concurrent_hermes_instances", lambda *_a, **_k: []
    )
