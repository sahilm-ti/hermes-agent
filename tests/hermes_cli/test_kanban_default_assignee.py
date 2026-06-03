"""Regression tests for #27145 — kanban.default_assignee for unassigned ready tasks.

When the dispatcher hits an unassigned ready task and ``kanban.default_assignee``
is set, the dispatcher applies the assignment and spawns. Without the config,
the task is skipped (existing behavior preserved).
"""
from __future__ import annotations

import json
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Spin up a fresh HERMES_HOME with a clean kanban DB.

    Hermeticity note: we do NOT ``del sys.modules[...]`` + reimport here.
    ``kanban_db`` resolves HERMES_HOME lazily at call time (via
    ``kanban_home()`` → ``get_default_hermes_root()``), and the per-test
    DB path is keyed off the fresh ``HERMES_HOME`` below, so a stale
    import cannot leak board state across tests. Reimporting would create
    a *second* ``hermes_cli.kanban_db`` module object: sibling test files
    that captured the module at import time (e.g.
    ``test_kanban_core_functionality.py``'s top-level ``import ... as kb``)
    would then read process-global state (``_recent_worker_exits``,
    ``_INITIALIZED_PATHS``) from the OLD module while a ``import ... as
    _kb`` inside the test resolves the NEW one — a non-hermetic split that
    flips dispatcher-contract assertions depending on test order.
    """
    test_home = tempfile.mkdtemp(prefix="kanban_default_assignee_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    from hermes_cli import kanban_db
    yield kanban_db, test_home
    # Cleanup is best-effort; tempfile dir survives but pytest isolation
    # gives each test its own monkeypatched HERMES_HOME so no cross-test
    # contamination.


def _fake_spawn(*args, **kwargs):
    """Stand-in for the real worker spawn — returns a fake PID."""
    return 12345




def test_unassigned_task_auto_assigned_with_default_assignee(isolated_kanban_home):
    """Core #27145 contract: with default_assignee set, an unassigned ready
    task gets the assignment applied and dispatched on the same tick. The
    DB row is mutated (assignee column + an 'assigned' event)."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee=None)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="default",
        )
    assert res.auto_assigned_default == [task_id]
    assert not res.skipped_unassigned
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == task_id
    assert res.spawned[0][1] == "default"

    with kb.connect_closing() as conn:
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["assignee"] == "default"

    # 'assigned' event emitted for the audit trail
    with kb.connect_closing() as conn:
        evs = list(conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = 'assigned'",
            (task_id,),
        ))
    assert len(evs) == 1
    payload = json.loads(evs[0][1])
    assert payload["assignee"] == "default"
    assert payload["source"] == "kanban.default_assignee"






def test_explicitly_assigned_task_untouched_by_default_assignee(isolated_kanban_home):
    """A task with an explicit assignee must NOT be touched by the
    default_assignee logic — that fallback only applies to genuinely
    unassigned rows."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee="default")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="someother",
        )
    assert task_id not in res.auto_assigned_default
    assert any(s[0] == task_id and s[1] == "default" for s in res.spawned)


