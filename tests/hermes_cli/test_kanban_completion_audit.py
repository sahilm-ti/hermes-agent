"""Tests for regime-B completion-audit functionality.

Covers:
- _maybe_schedule_completion_audit sets / skips completion_audit_at
- claim_completion_audit_task atomically claims and prevents double-audit
- complete_completion_audit closes out the audit run
- dispatch_once completion-audit column dispatch (audited list)
- Existing kanban_review (PR) flow is unchanged
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _complete_task_no_pr(conn, task_id):
    """Helper: claim and complete a task with no PR so audit gets scheduled."""
    kb.claim_task(conn, task_id)
    kb.complete_task(conn, task_id, summary="all done, nothing special")


def _add_pr_comment(conn, task_id):
    """Helper: add a PR-URL comment so the task is excluded from audit."""
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (task_id, "alice", "PR https://github.com/owner/repo/pull/42 merged", int(time.time())),
    )


# ---------------------------------------------------------------------------
# _maybe_schedule_completion_audit
# ---------------------------------------------------------------------------


def test_completion_audit_scheduled_for_no_pr_task(kanban_home):
    """A task completed without a PR gets completion_audit_at set."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="sync fork", assignee="alice")
        _complete_task_no_pr(conn, t)
        row = conn.execute(
            "SELECT completion_audit_at FROM tasks WHERE id = ?", (t,)
        ).fetchone()
    assert row["completion_audit_at"] is not None


def test_completion_audit_skipped_for_pr_task(kanban_home):
    """A task that has a PR comment does NOT get completion_audit_at set."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="feature PR", assignee="alice")
        _add_pr_comment(conn, t)
        _complete_task_no_pr(conn, t)
        row = conn.execute(
            "SELECT completion_audit_at FROM tasks WHERE id = ?", (t,)
        ).fetchone()
    assert row["completion_audit_at"] is None


def test_completion_audit_skipped_for_skip_review_body(kanban_home):
    """A task with 'skip-review: reason' in the body opts out of audit."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="typo fix",
            assignee="alice",
            body="Fix a trivial typo.\n\nskip-review: typo only, no audit needed",
        )
        _complete_task_no_pr(conn, t)
        row = conn.execute(
            "SELECT completion_audit_at FROM tasks WHERE id = ?", (t,)
        ).fetchone()
    assert row["completion_audit_at"] is None


def test_completion_audit_scheduling_idempotent(kanban_home):
    """Calling _maybe_schedule_completion_audit twice does not change the timestamp."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="twice", assignee="alice")
        _complete_task_no_pr(conn, t)
        first_ts = conn.execute(
            "SELECT completion_audit_at FROM tasks WHERE id = ?", (t,)
        ).fetchone()["completion_audit_at"]
        kb._maybe_schedule_completion_audit(conn, t)
        second_ts = conn.execute(
            "SELECT completion_audit_at FROM tasks WHERE id = ?", (t,)
        ).fetchone()["completion_audit_at"]
    assert first_ts == second_ts


# ---------------------------------------------------------------------------
# claim_completion_audit_task
# ---------------------------------------------------------------------------


def test_claim_completion_audit_task_clears_trigger(kanban_home):
    """Claiming an audit task atomically clears completion_audit_at."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="investigate foo", assignee="alice")
        _complete_task_no_pr(conn, t)
        # Verify trigger is set
        assert (
            conn.execute(
                "SELECT completion_audit_at FROM tasks WHERE id = ?", (t,)
            ).fetchone()["completion_audit_at"]
            is not None
        )
        claimed = kb.claim_completion_audit_task(conn, t)
    assert claimed is not None
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT status, completion_audit_at, claim_lock FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
    # Task stays done; trigger cleared; claim held.
    assert row["status"] == "done"
    assert row["completion_audit_at"] is None
    assert row["claim_lock"] is not None


def test_claim_completion_audit_task_prevents_double_claim(kanban_home):
    """A second concurrent claim attempt returns None."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="no double audit", assignee="alice")
        _complete_task_no_pr(conn, t)
        first = kb.claim_completion_audit_task(conn, t)
        second = kb.claim_completion_audit_task(conn, t)
    assert first is not None
    assert second is None


def test_claim_completion_audit_task_returns_none_when_not_scheduled(kanban_home):
    """Tasks without completion_audit_at set cannot be claimed for audit."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="PR task", assignee="alice")
        _add_pr_comment(conn, t)
        _complete_task_no_pr(conn, t)
        claimed = kb.claim_completion_audit_task(conn, t)
    assert claimed is None


def test_claim_completion_audit_task_creates_run_row(kanban_home):
    """Claiming an audit task creates a task_run entry."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="audit run", assignee="alice")
        _complete_task_no_pr(conn, t)
        claimed = kb.claim_completion_audit_task(conn, t)
    assert claimed is not None
    with kb.connect() as conn:
        runs = conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at DESC",
            (t,),
        ).fetchall()
    # The audit should have created a new run distinct from the original.
    assert len(runs) >= 1
    latest = runs[0]
    assert latest["status"] == "running"


# ---------------------------------------------------------------------------
# complete_completion_audit
# ---------------------------------------------------------------------------


def test_complete_completion_audit_releases_claim(kanban_home):
    """complete_completion_audit releases the claim lock and keeps status=done."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="audit close", assignee="alice")
        _complete_task_no_pr(conn, t)
        lock = "audit-lock-test"
        claimed = kb.claim_completion_audit_task(conn, t, claimer=lock)
        assert claimed is not None
        ok = kb.complete_completion_audit(conn, t, summary="all good", claimer=lock)
    assert ok is True
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT status, claim_lock, completion_audit_at FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
    assert row["status"] == "done"
    assert row["claim_lock"] is None
    assert row["completion_audit_at"] is None


def test_complete_completion_audit_emits_event(kanban_home):
    """complete_completion_audit records a completion_audit_done event."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="audit event", assignee="alice")
        _complete_task_no_pr(conn, t)
        lock = "lock-evt"
        kb.claim_completion_audit_task(conn, t, claimer=lock)
        kb.complete_completion_audit(conn, t, summary="pass", claimer=lock)
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND kind = 'completion_audit_done'",
            (t,),
        ).fetchall()
    assert len(events) == 1


# ---------------------------------------------------------------------------
# dispatch_once — completion-audit column
# ---------------------------------------------------------------------------


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


def test_dispatch_completion_audit_dry_run(kanban_home, all_assignees_spawnable):
    """dispatch_once dry-run sees completion-audit tasks and reports them in audited."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="investigate leak", assignee="alice")
        _complete_task_no_pr(conn, t)
        res = kb.dispatch_once(conn, dry_run=True)
    assert any(a[0] == t for a in res.audited)


def test_dispatch_completion_audit_spawns(kanban_home, all_assignees_spawnable):
    """dispatch_once spawns an audit agent for completion-audit tasks."""
    spawned_tasks = []

    def capture_spawn(task, workspace, board=None):
        spawned_tasks.append(task)
        return 99

    with kb.connect() as conn:
        t = kb.create_task(conn, title="explore landscape", assignee="alice")
        _complete_task_no_pr(conn, t)
        res = kb.dispatch_once(conn, spawn_fn=capture_spawn)
    assert any(a[0] == t for a in res.audited)
    assert len(spawned_tasks) == 1
    assert spawned_tasks[0].skills == ["sdlc-completion-audit"]


def test_dispatch_completion_audit_clears_trigger(kanban_home, all_assignees_spawnable):
    """After dispatch spawns an audit agent, completion_audit_at is NULL."""
    def fake_spawn(task, workspace, board=None):
        return 100

    with kb.connect() as conn:
        t = kb.create_task(conn, title="memory cleanup", assignee="alice")
        _complete_task_no_pr(conn, t)
        kb.dispatch_once(conn, spawn_fn=fake_spawn)
        row = conn.execute(
            "SELECT completion_audit_at FROM tasks WHERE id = ?", (t,)
        ).fetchone()
    assert row["completion_audit_at"] is None


def test_dispatch_completion_audit_no_double_spawn(kanban_home, all_assignees_spawnable):
    """A second dispatch tick does NOT re-spawn an already-claimed audit."""
    spawn_count = [0]

    def counting_spawn(task, workspace, board=None):
        spawn_count[0] += 1
        return 101

    with kb.connect() as conn:
        t = kb.create_task(conn, title="idempotent audit", assignee="alice")
        _complete_task_no_pr(conn, t)
        kb.dispatch_once(conn, spawn_fn=counting_spawn)
        # Second tick — trigger already cleared.
        kb.dispatch_once(conn, spawn_fn=counting_spawn)
    assert spawn_count[0] == 1


def test_dispatch_completion_audit_counts_toward_max_spawn(
    kanban_home, all_assignees_spawnable
):
    """Completion-audit spawns count against max_spawn."""
    spawned = []

    def capture(task, workspace, board=None):
        spawned.append(task.id)
        return 42

    with kb.connect() as conn:
        t1 = kb.create_task(conn, title="audit 1", assignee="alice")
        t2 = kb.create_task(conn, title="audit 2", assignee="alice")
        _complete_task_no_pr(conn, t1)
        _complete_task_no_pr(conn, t2)
        res = kb.dispatch_once(conn, spawn_fn=capture, max_spawn=1)
    # max_spawn=1 → only one audit spawned.
    assert len(res.audited) == 1
    assert len(spawned) == 1


# ---------------------------------------------------------------------------
# Existing kanban_review (PR) flow is UNCHANGED
# ---------------------------------------------------------------------------


def test_review_flow_unchanged_with_audit_present(kanban_home, all_assignees_spawnable):
    """A PR card in review status still spawns the sdlc-review skill, not audit."""
    spawned_tasks = []

    def capture(task, workspace, board=None):
        spawned_tasks.append(task)
        return 200

    with kb.connect() as conn:
        pr_task = kb.create_task(conn, title="feat: add feature", assignee="alice")
        # Manually push to review status (simulates kanban_review call).
        conn.execute(
            "UPDATE tasks SET status = 'review' WHERE id = ?", (pr_task,)
        )
        res = kb.dispatch_once(conn, spawn_fn=capture)
    assert len(res.spawned) == 1
    assert spawned_tasks[0].skills == ["sdlc-review"]
    # audited list is empty for PR cards.
    assert res.audited == []


def test_pr_task_not_audited_when_completed(kanban_home, all_assignees_spawnable):
    """A task with a PR URL in comments does not get scheduled for audit."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="fix: bug", assignee="alice")
        _add_pr_comment(conn, t)
        _complete_task_no_pr(conn, t)
        res = kb.dispatch_once(conn, dry_run=True)
    assert res.audited == []


# ---------------------------------------------------------------------------
# Schema: completion_audit_at column exists after init
# ---------------------------------------------------------------------------


def test_schema_has_completion_audit_at(kanban_home):
    """init_db creates the completion_audit_at column on tasks."""
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "completion_audit_at" in cols
