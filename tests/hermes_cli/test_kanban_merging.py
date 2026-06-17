"""Tests for the post-approve PR merger flow.

Covers:
- ``_extract_pr_url``: event path, comment path, no-URL path
- ``claim_merger_task``: human_review → merging transition, merge_requested event
- ``approve_task`` routing:
  - PR URL present, skip_merge unset → merge_triggered, task in ``merging``
  - PR URL present, skip_merge set → done, NO merger
  - No PR URL → done (backwards-compat path)
- ``merging`` landing: complete_task (→ done) and block_task (→ blocked)
- ``recover_stuck_merging``: dead/stalled merger → blocked, healthy → untouched
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claim(conn, tid):
    task = kb.claim_task(conn, tid)
    assert task is not None
    return task


def _make_human_review_task(conn, *, title="pr task", assignee="worker"):
    """Create a task that has progressed through claim → review → human_review."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    _claim(conn, tid)
    kb.move_to_review(conn, tid, reason="PR at https://github.com/owner/repo/pull/42")
    _ = kb.claim_review_task(conn, tid)
    kb.move_to_human_review(conn, tid, reason="AC verified")
    return tid


# ---------------------------------------------------------------------------
# _extract_pr_url
# ---------------------------------------------------------------------------


class TestExtractPrUrl:
    def test_finds_url_in_review_requested_event(self, kanban_home):
        """URL carried in review_requested event.reason is returned."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="x", assignee="worker")
            _claim(conn, tid)
            kb.move_to_review(
                conn, tid, reason="PR merged: https://github.com/org/repo/pull/99"
            )
            url = kb._extract_pr_url(conn, tid)
        assert url == "https://github.com/org/repo/pull/99"

    def test_falls_back_to_comments(self, kanban_home):
        """If no review_requested event, scans comments."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="x", assignee="worker")
            kb.add_comment(
                conn,
                tid,
                "worker",
                "Opened PR: https://github.com/org/repo/pull/7",
            )
            url = kb._extract_pr_url(conn, tid)
        assert url == "https://github.com/org/repo/pull/7"

    def test_returns_none_when_no_url(self, kanban_home):
        """Returns None when neither events nor comments carry a PR URL."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="x", assignee="worker")
            _claim(conn, tid)
            # A review_requested event with no URL in the reason.
            kb.move_to_review(conn, tid, reason="work done, no PR")
            url = kb._extract_pr_url(conn, tid)
        assert url is None

    def test_event_path_takes_priority_over_comments(self, kanban_home):
        """Event-path URL wins over a comment URL (events scanned first)."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="x", assignee="worker")
            _claim(conn, tid)
            # Comment has a different PR number.
            kb.add_comment(
                conn,
                tid,
                "worker",
                "Old PR: https://github.com/org/repo/pull/1",
            )
            # Event has the newer PR.
            kb.move_to_review(
                conn, tid, reason="PR https://github.com/org/repo/pull/55"
            )
            url = kb._extract_pr_url(conn, tid)
        # Should return the event URL, not the comment URL.
        assert url == "https://github.com/org/repo/pull/55"


# ---------------------------------------------------------------------------
# claim_merger_task
# ---------------------------------------------------------------------------


class TestClaimMergerTask:
    def test_human_review_to_merging(self, kanban_home):
        """claim_merger_task transitions human_review → merging."""
        with kb.connect() as conn:
            tid = _make_human_review_task(conn)
            task = kb.claim_merger_task(
                conn, tid, pr_url="https://github.com/org/repo/pull/10"
            )
        assert task is not None
        assert task.status == "merging"

    def test_appends_merge_requested_event(self, kanban_home):
        """A merge_requested event is appended carrying the pr_url."""
        pr = "https://github.com/org/repo/pull/10"
        with kb.connect() as conn:
            tid = _make_human_review_task(conn)
            kb.claim_merger_task(conn, tid, pr_url=pr)
            events = kb.list_events(conn, tid)
        merge_events = [e for e in events if e.kind == "merge_requested"]
        assert len(merge_events) == 1
        payload = merge_events[0].payload or {}
        assert payload.get("pr_url") == pr

    def test_creates_new_run_row(self, kanban_home):
        """claim_merger_task creates a new task_run entry."""
        with kb.connect() as conn:
            tid = _make_human_review_task(conn)
            task = kb.claim_merger_task(
                conn, tid, pr_url="https://github.com/org/repo/pull/10"
            )
        assert task is not None
        assert task.current_run_id is not None

    def test_returns_none_when_already_claimed(self, kanban_home):
        """Returns None when the task has a claim lock (already running)."""
        with kb.connect() as conn:
            tid = _make_human_review_task(conn)
            # First claim succeeds.
            task1 = kb.claim_merger_task(
                conn, tid, pr_url="https://github.com/org/repo/pull/10"
            )
            assert task1 is not None
            # Second claim on the running task must fail.
            task2 = kb.claim_merger_task(
                conn, tid, pr_url="https://github.com/org/repo/pull/10"
            )
        assert task2 is None

    def test_returns_none_when_not_human_review(self, kanban_home):
        """Returns None when task is not in human_review status."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="x", assignee="worker")
            _claim(conn, tid)
            # Still running — not human_review.
            task = kb.claim_merger_task(
                conn, tid, pr_url="https://github.com/org/repo/pull/1"
            )
        assert task is None


# ---------------------------------------------------------------------------
# approve_task routing
# ---------------------------------------------------------------------------


class TestApproveTaskRouting:
    def test_routes_to_merge_triggered_when_pr_url_present(self, kanban_home):
        """When a PR URL exists in events, approve_task routes to merge_triggered."""
        with kb.connect() as conn:
            tid = _make_human_review_task(conn)
            ok, outcome, pr_url, task = kb.approve_task(conn, tid)
        assert ok
        assert outcome == "merge_triggered"
        assert pr_url == "https://github.com/owner/repo/pull/42"
        assert task is not None
        assert task.status == "merging"

    def test_routes_to_done_when_no_pr_url(self, kanban_home):
        """When no PR URL is found, approve_task routes to done (backwards-compat)."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="no-pr task", assignee="worker")
            _claim(conn, tid)
            kb.move_to_human_review(conn, tid, reason="research deliverable, no PR")
            ok, outcome, pr_url, task = kb.approve_task(conn, tid)
        assert ok
        assert outcome == "done"
        assert pr_url is None
        assert task is None
        with kb.connect() as conn:
            t = kb.get_task(conn, tid)
        assert t is not None
        assert t.status == "done"
        assert t.completed_at is not None

    def test_done_path_emits_approved_event(self, kanban_home):
        """No-PR path emits an approved event (unchanged behavior)."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="x", assignee="worker")
            _claim(conn, tid)
            kb.move_to_human_review(conn, tid, reason="no PR")
            kb.approve_task(conn, tid, reason="LGTM")
            events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "approved" in kinds

    def test_merge_triggered_path_emits_merge_requested_event(self, kanban_home):
        """PR path emits a merge_requested event with the pr_url."""
        with kb.connect() as conn:
            tid = _make_human_review_task(conn)
            kb.approve_task(conn, tid)
            events = kb.list_events(conn, tid)
        merge_events = [e for e in events if e.kind == "merge_requested"]
        assert len(merge_events) >= 1
        last_payload = merge_events[-1].payload or {}
        assert "github.com" in last_payload.get("pr_url", "")

    def test_returns_not_ok_when_not_human_review(self, kanban_home):
        """Returns (False, ...) when task is not in human_review."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="x", assignee="worker")
            _claim(conn, tid)
            # Still running.
            ok, outcome, _, __ = kb.approve_task(conn, tid)
        assert not ok
        assert outcome == "not_found"

    def test_idempotent_second_approve_fails(self, kanban_home):
        """A second approve on an already-transitioned task returns not-ok."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="x", assignee="worker")
            _claim(conn, tid)
            kb.move_to_human_review(conn, tid, reason="no PR")
            ok1, _, __, ___ = kb.approve_task(conn, tid)
            assert ok1
            # Second call — task is already done.
            ok2, _, __, ___ = kb.approve_task(conn, tid)
            assert not ok2


# ---------------------------------------------------------------------------
# skip_merge flag (opt-out)
# ---------------------------------------------------------------------------


class TestSkipMerge:
    def test_skip_merge_pr_card_goes_straight_to_done(self, kanban_home):
        """skip_merge set + PR present → done directly, NO merger claim."""
        with kb.connect() as conn:
            tid = _make_human_review_task(conn)
            assert kb.set_skip_merge(conn, tid, value=True) is True
            ok, outcome, pr_url, task = kb.approve_task(conn, tid)
        assert ok
        assert outcome == "done"
        # No merger task returned, no PR routing.
        assert task is None
        assert pr_url is None
        with kb.connect() as conn:
            t = kb.get_task(conn, tid)
        assert t is not None
        assert t.status == "done"
        assert t.completed_at is not None

    def test_skip_merge_spawns_no_merge_requested_event(self, kanban_home):
        """skip_merge path emits NO merge_requested event (no merger)."""
        with kb.connect() as conn:
            tid = _make_human_review_task(conn)
            kb.set_skip_merge(conn, tid, value=True)
            kb.approve_task(conn, tid)
            events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "merge_requested" not in kinds
        assert "approved" in kinds

    def test_skip_merge_records_audit_on_approved_event(self, kanban_home):
        """The approved event records skip_merge + pr_url for audit."""
        with kb.connect() as conn:
            tid = _make_human_review_task(conn)
            kb.set_skip_merge(conn, tid, value=True)
            kb.approve_task(conn, tid, reason="land manually")
            events = kb.list_events(conn, tid)
        approved = [e for e in events if e.kind == "approved"]
        assert approved
        payload = approved[-1].payload or {}
        assert payload.get("skip_merge") is True
        assert "github.com" in (payload.get("pr_url") or "")

    def test_skip_merge_unset_still_routes_to_merging(self, kanban_home):
        """Default (flag unset) preserves the merging-with-visibility path."""
        with kb.connect() as conn:
            tid = _make_human_review_task(conn)
            ok, outcome, pr_url, task = kb.approve_task(conn, tid)
        assert ok
        assert outcome == "merge_triggered"
        assert task is not None
        assert task.status == "merging"

    def test_set_skip_merge_persists_and_hydrates(self, kanban_home):
        """set_skip_merge persists to the column and hydrates on get_task."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="x", assignee="worker")
            assert kb.get_task(conn, tid).skip_merge is False
            kb.set_skip_merge(conn, tid, value=True)
            assert kb.get_task(conn, tid).skip_merge is True
            kb.set_skip_merge(conn, tid, value=False)
            assert kb.get_task(conn, tid).skip_merge is False


# ---------------------------------------------------------------------------
# merging → done / merging → blocked landing transitions
# ---------------------------------------------------------------------------


class TestMergingLanding:
    def _make_merging_task(self, conn):
        tid = _make_human_review_task(conn)
        task = kb.claim_merger_task(
            conn, tid, pr_url="https://github.com/org/repo/pull/10"
        )
        assert task is not None and task.status == "merging"
        return tid

    def test_merging_to_done_via_complete(self, kanban_home):
        """The merger lands a successful merge: merging → done."""
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            ok = kb.complete_task(conn, tid, summary="merged PR #10")
            assert ok
            t = kb.get_task(conn, tid)
        assert t.status == "done"
        assert t.completed_at is not None

    def test_merging_to_blocked_via_block(self, kanban_home):
        """The merger hits a genuine blocker: merging → blocked with reason."""
        reason = "merge conflict in foo.py — cannot auto-resolve"
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            ok = kb.block_task(conn, tid, reason=reason)
            assert ok
            t = kb.get_task(conn, tid)
            events = kb.list_events(conn, tid)
        assert t.status == "blocked"
        blocked = [e for e in events if e.kind == "blocked"]
        assert blocked
        assert (blocked[-1].payload or {}).get("reason") == reason


# ---------------------------------------------------------------------------
# recover_stuck_merging reaper (the safety net)
# ---------------------------------------------------------------------------


class TestRecoverStuckMerging:
    def _make_merging_task(self, conn):
        tid = _make_human_review_task(conn)
        task = kb.claim_merger_task(
            conn, tid, pr_url="https://github.com/org/repo/pull/10"
        )
        assert task is not None and task.status == "merging"
        return tid

    def test_dead_pid_merging_task_goes_to_blocked(self, kanban_home):
        """A merging task whose merger PID is dead → blocked with a reason."""
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            # Stamp a host-local claim + a definitely-dead PID.
            host = kb._claimer_id().split(":", 1)[0]
            conn.execute(
                "UPDATE tasks SET claim_lock = ?, worker_pid = ? WHERE id = ?",
                (f"{host}:999999", 2147483646, tid),
            )
            conn.commit()
            recovered = kb.recover_stuck_merging(conn, signal_fn=lambda *a: None)
            assert tid in recovered
            t = kb.get_task(conn, tid)
            events = kb.list_events(conn, tid)
        assert t.status == "blocked"
        assert t.claim_lock is None
        kinds = [e.kind for e in events]
        assert "merge_failed" in kinds
        assert "blocked" in kinds
        blocked = [e for e in events if e.kind == "blocked"]
        assert "merger died/stalled" in (blocked[-1].payload or {}).get("reason", "")

    def test_healthy_heartbeating_merging_task_untouched(self, kanban_home):
        """A merging task with a live PID + fresh heartbeat is left alone."""
        import os
        import time as _time

        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            host = kb._claimer_id().split(":", 1)[0]
            # Live PID (this test process) + a fresh heartbeat.
            conn.execute(
                "UPDATE tasks SET claim_lock = ?, worker_pid = ?, "
                "last_heartbeat_at = ? WHERE id = ?",
                (f"{host}:1", os.getpid(), int(_time.time()), tid),
            )
            conn.commit()
            recovered = kb.recover_stuck_merging(conn, signal_fn=lambda *a: None)
            assert tid not in recovered
            t = kb.get_task(conn, tid)
        assert t.status == "merging"

    def test_other_host_merging_task_ignored(self, kanban_home):
        """A merging task claimed by another host is not recovered locally."""
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            conn.execute(
                "UPDATE tasks SET claim_lock = ?, worker_pid = ? WHERE id = ?",
                ("other-host:123", 2147483646, tid),
            )
            conn.commit()
            recovered = kb.recover_stuck_merging(conn, signal_fn=lambda *a: None)
            assert tid not in recovered
            t = kb.get_task(conn, tid)
        assert t.status == "merging"

    def test_alive_merger_past_max_runtime_goes_to_blocked(self, kanban_home):
        """A merger that is alive AND heartbeating but has run past its
        per-task max_runtime_seconds is recovered to 'blocked' (not 'ready' —
        re-queueing risks a double-merge). This closes the runtime-cap gap:
        enforce_max_runtime is scoped to 'running' and skips 'merging', so the
        merger's runtime cap is enforced here.
        """
        import os
        import time as _time

        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            host = kb._claimer_id().split(":", 1)[0]
            now = int(_time.time())
            # Live PID (this process) + FRESH heartbeat → not dead, not stalled.
            # Started 100s ago with a 10s cap → over the runtime cap.
            conn.execute(
                "UPDATE tasks SET claim_lock = ?, worker_pid = ?, "
                "last_heartbeat_at = ?, max_runtime_seconds = ?, "
                "started_at = ? WHERE id = ?",
                (f"{host}:1", os.getpid(), now, 10, now - 100, tid),
            )
            # Also stamp the active run's started_at, since recover_stuck_merging
            # measures elapsed from COALESCE(run.started_at, task.started_at).
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (now - 100, tid),
            )
            conn.commit()
            recovered = kb.recover_stuck_merging(conn, signal_fn=lambda *a: None)
            assert tid in recovered
            t = kb.get_task(conn, tid)
            events = kb.list_events(conn, tid)
        assert t.status == "blocked"
        assert t.claim_lock is None
        kinds = [e.kind for e in events]
        assert "merge_failed" in kinds
        assert "blocked" in kinds
        blocked = [e for e in events if e.kind == "blocked"]
        reason = (blocked[-1].payload or {}).get("reason", "")
        assert "max_runtime_seconds" in reason
        merge_failed = [e for e in events if e.kind == "merge_failed"]
        assert (merge_failed[-1].payload or {}).get("runtime_exceeded") is True

    def test_alive_merger_within_max_runtime_untouched(self, kanban_home):
        """A merger that is alive, heartbeating, and still within its runtime
        cap is left alone (the runtime trigger must not fire early)."""
        import os
        import time as _time

        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            host = kb._claimer_id().split(":", 1)[0]
            now = int(_time.time())
            # Started 5s ago with a 3600s cap → well within the runtime cap.
            conn.execute(
                "UPDATE tasks SET claim_lock = ?, worker_pid = ?, "
                "last_heartbeat_at = ?, max_runtime_seconds = ?, "
                "started_at = ? WHERE id = ?",
                (f"{host}:1", os.getpid(), now, 3600, now - 5, tid),
            )
            conn.commit()
            recovered = kb.recover_stuck_merging(conn, signal_fn=lambda *a: None)
            assert tid not in recovered
            t = kb.get_task(conn, tid)
        assert t.status == "merging"


# ---------------------------------------------------------------------------
# status enum + dashboard column registration
# ---------------------------------------------------------------------------


class TestMergingStatusRegistration:
    def test_merging_in_valid_statuses(self, kanban_home):
        assert "merging" in kb.VALID_STATUSES

    def test_merging_in_worker_active_statuses(self, kanban_home):
        assert "merging" in kb.WORKER_ACTIVE_STATUSES

    def test_merging_in_board_columns_between_human_review_and_done(self):
        from plugins.kanban.dashboard import plugin_api

        cols = plugin_api.BOARD_COLUMNS
        assert "merging" in cols
        assert cols.index("human_review") < cols.index("merging") < cols.index("done")


# ---------------------------------------------------------------------------
# heartbeat liveness in `merging` (regression: heartbeat_worker /
# heartbeat_claim were scoped to status='running' only, so a merging worker
# could never write last_heartbeat_at / extend its claim — the reaper then
# force-blocked a still-healthy in-flight merger once its claim TTL expired)
# ---------------------------------------------------------------------------


class TestMergingHeartbeat:
    def _make_merging_task(self, conn):
        tid = _make_human_review_task(conn)
        task = kb.claim_merger_task(
            conn, tid, pr_url="https://github.com/org/repo/pull/10"
        )
        assert task is not None and task.status == "merging"
        return tid

    def test_heartbeat_worker_succeeds_in_merging(self, kanban_home):
        """heartbeat_worker through the real API bumps last_heartbeat_at on a
        merging task (not just running). Pre-fix this returned False and the
        column stayed NULL."""
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            # claim_merger_task leaves last_heartbeat_at unset.
            before = kb.get_task(conn, tid)
            assert before is not None
            assert before.last_heartbeat_at is None
            ok = kb.heartbeat_worker(conn, tid, note="merging along")
            assert ok is True
            after = kb.get_task(conn, tid)
            assert after is not None
        assert after.status == "merging"
        assert after.last_heartbeat_at is not None

    def test_heartbeat_worker_expected_run_id_in_merging(self, kanban_home):
        """The expected_run_id variant (used by the tool-layer auto-heartbeat)
        also succeeds for a merging task."""
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            claimed = kb.get_task(conn, tid)
            assert claimed is not None
            run_id = claimed.current_run_id
            assert run_id is not None
            ok = kb.heartbeat_worker(conn, tid, expected_run_id=run_id)
            assert ok is True
            after = kb.get_task(conn, tid)
            assert after is not None
        assert after.last_heartbeat_at is not None

    def test_heartbeat_claim_extends_merging_claim(self, kanban_home):
        """heartbeat_claim extends claim_expires for a merging task. Pre-fix it
        was scoped to running, so a merging claim never advanced past its TTL."""
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            claimed = kb.get_task(conn, tid)
            assert claimed is not None
            lock = claimed.claim_lock
            # Force the claim near expiry, then extend via the real API.
            conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (int(time.time()) - 1, tid),
            )
            conn.commit()
            ok = kb.heartbeat_claim(conn, tid, claimer=lock)
            assert ok is True
            after = kb.get_task(conn, tid)
            assert after is not None
        assert after.claim_expires is not None
        assert after.claim_expires > int(time.time())

    def test_real_heartbeat_protects_merger_past_claim_ttl(self, kanban_home):
        """End-to-end regression for the rejected bug: a healthy merger running
        past its initial 15-min claim TTL must NOT be force-blocked, as long as
        it keeps heartbeating through the real API. Pre-fix, heartbeat_worker
        no-oped in merging (last_heartbeat_at stayed NULL); once the claim TTL
        expired the reaper hit the 'never heartbeated and its claim expired'
        branch and blocked the live worker. With the fix the heartbeat lands,
        so the reaper sees a fresh heartbeat and leaves it alone."""
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            host = kb._claimer_id().split(":", 1)[0]
            # Live PID (this test process); claim TTL already in the past
            # (simulating a merger that's been running >15 min).
            conn.execute(
                "UPDATE tasks SET claim_lock = ?, worker_pid = ?, "
                "claim_expires = ? WHERE id = ?",
                (f"{host}:1", os.getpid(), int(time.time()) - 1, tid),
            )
            conn.commit()
            # The merger heartbeats through the real API (as it would via
            # kanban_heartbeat / the auto-heartbeat bridge).
            assert kb.heartbeat_worker(conn, tid) is True
            # Now the reaper runs: fresh heartbeat → healthy → left alone.
            recovered = kb.recover_stuck_merging(conn, signal_fn=lambda *a: None)
            assert tid not in recovered
            t = kb.get_task(conn, tid)
            assert t is not None
        assert t.status == "merging"

    def test_merger_never_heartbeating_past_ttl_is_blocked(self, kanban_home):
        """The flip side: a merger that genuinely never heartbeats and whose
        claim TTL has expired (and whose worker is gone) is still recovered to
        blocked (the fix must not defang the safety net for a dead/stalled
        merger)."""
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            host = kb._claimer_id().split(":", 1)[0]
            # No PID, never heartbeated, claim TTL in the past.
            conn.execute(
                "UPDATE tasks SET claim_lock = ?, worker_pid = NULL, "
                "last_heartbeat_at = NULL, claim_expires = ? WHERE id = ?",
                (f"{host}:1", int(time.time()) - 1, tid),
            )
            conn.commit()
            recovered = kb.recover_stuck_merging(conn, signal_fn=lambda *a: None)
            assert tid in recovered
            t = kb.get_task(conn, tid)
            assert t is not None
            events = kb.list_events(conn, tid)
        assert t.status == "blocked"
        blocked = [e for e in events if e.kind == "blocked"]
        assert "merger died/stalled" in (blocked[-1].payload or {}).get("reason", "")
