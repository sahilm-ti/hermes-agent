"""Tests for the post-approve PR merger flow.

Covers:
- ``_extract_pr_url``: event path, comment path, no-URL path
- ``claim_merger_task``: human_review → merging transition, merge_requested event
- ``approve_task`` routing:
  - PR URL present, skip_merge unset → merge_triggered, task in ``merging``
  - PR URL present, skip_merge set → done, NO merger
  - No PR URL → done (backwards-compat path)
- ``merging`` landing: complete_task (→ done) and block_task (→ blocked)
- ``merging`` recovery: NO reaper / dispatcher automation touches ``merging``;
  the merger is the sole authority to leave it (→ done or → blocked). A dead
  merger leaves the card stuck in ``merging`` indefinitely (intentional).
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
# merging is NOT reaped — the merger is the sole authority to leave `merging`
#
# Sahil's explicit decision (follow-up to PR #51): remove the merging-status
# reaper entirely. NO background process (reaper / stale-claim / runtime-cap /
# crash detection) may move a card out of `merging`. The merger itself either
# completes (→ done) or self-blocks (→ blocked). A merger whose process
# genuinely dies leaves the card stuck in `merging` until a human intervenes —
# that tradeoff is intentional. These tests are the inverse of the old
# TestRecoverStuckMerging suite: they assert that dead/stalled/over-runtime
# mergers are LEFT ALONE.
# ---------------------------------------------------------------------------


class TestMergingNotReaped:
    def _make_merging_task(self, conn):
        tid = _make_human_review_task(conn)
        task = kb.claim_merger_task(
            conn, tid, pr_url="https://github.com/org/repo/pull/10"
        )
        assert task is not None and task.status == "merging"
        return tid

    def test_recover_stuck_merging_is_gone(self):
        """The dedicated merging reaper must not exist any more — its very
        presence is the bug this card removes."""
        assert not hasattr(kb, "recover_stuck_merging")

    def test_dispatch_result_has_no_recovered_merging_field(self):
        """DispatchResult must not carry a recovered_merging field — nothing
        recovers merging cards, so there is nothing to report."""
        result = kb.DispatchResult()
        assert not hasattr(result, "recovered_merging")

    def test_dead_pid_merging_task_stays_in_merging(self, kanban_home):
        """A merging task whose merger PID is dead is NOT auto-bounced — it
        stays in `merging` indefinitely (a human must intervene)."""
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            host = kb._claimer_id().split(":", 1)[0]
            # Host-local claim + a definitely-dead PID.
            conn.execute(
                "UPDATE tasks SET claim_lock = ?, worker_pid = ? WHERE id = ?",
                (f"{host}:999999", 2147483646, tid),
            )
            conn.commit()
            # A full dispatch tick (which used to run the merging reaper) must
            # leave the card untouched.
            kb.dispatch_once(conn, dry_run=True)
            t = kb.get_task(conn, tid)
        assert t.status == "merging"
        assert t.claim_lock == f"{host}:999999"

    def test_never_heartbeating_merging_past_ttl_stays_in_merging(self, kanban_home):
        """A merger that never heartbeated and whose claim TTL has expired is
        still left in `merging` (the old reaper bounced this to blocked)."""
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            host = kb._claimer_id().split(":", 1)[0]
            conn.execute(
                "UPDATE tasks SET claim_lock = ?, worker_pid = NULL, "
                "last_heartbeat_at = NULL, claim_expires = ? WHERE id = ?",
                (f"{host}:1", int(time.time()) - 1, tid),
            )
            conn.commit()
            kb.dispatch_once(conn, dry_run=True)
            t = kb.get_task(conn, tid)
        assert t.status == "merging"

    def test_alive_merger_past_max_runtime_stays_in_merging(self, kanban_home):
        """A merger alive + heartbeating but wedged past its max_runtime_seconds
        is NOT auto-bounced. enforce_max_runtime is scoped to `running` and
        skips `merging`; with the reaper gone nothing enforces the cap on a
        merging worker — it owns its own exit."""
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            host = kb._claimer_id().split(":", 1)[0]
            now = int(time.time())
            # Live PID + fresh heartbeat, started 100s ago with a 10s cap.
            conn.execute(
                "UPDATE tasks SET claim_lock = ?, worker_pid = ?, "
                "last_heartbeat_at = ?, max_runtime_seconds = ?, "
                "started_at = ? WHERE id = ?",
                (f"{host}:1", os.getpid(), now, 10, now - 100, tid),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (now - 100, tid),
            )
            conn.commit()
            kb.dispatch_once(conn, dry_run=True)
            t = kb.get_task(conn, tid)
        assert t.status == "merging"

    def test_merger_can_complete_from_merging(self, kanban_home):
        """The merger's own success path: complete_task moves merging → done."""
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            ok = kb.complete_task(
                conn, tid, summary="Merged PR #10 (squash+delete-branch)"
            )
            assert ok is True
            t = kb.get_task(conn, tid)
        assert t.status == "done"

    def test_merger_can_block_from_merging(self, kanban_home):
        """The merger's own give-up path: block_task moves merging → blocked
        with the reason carried through to the board."""
        reason = "Merge conflict on PR #10 — needs manual rebase. Files: a.py"
        with kb.connect() as conn:
            tid = self._make_merging_task(conn)
            ok = kb.block_task(conn, tid, reason=reason)
            assert ok is True
            t = kb.get_task(conn, tid)
            events = kb.list_events(conn, tid)
        assert t.status == "blocked"
        blocked = [e for e in events if e.kind == "blocked"]
        assert blocked
        assert (blocked[-1].payload or {}).get("reason") == reason


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
# could never write last_heartbeat_at / extend its claim). Nothing reaps a
# `merging` card — the merger is the sole authority to leave it — but the
# heartbeat path must still WORK so a live merger can signal liveness and
# extend its claim if it chooses to.
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


# ---------------------------------------------------------------------------
# Tool-layer skip_merge arg parsing (tools/kanban_tools.py _handle_approve)
#
# The tool surface receives `skip_merge` from the model as a JSON value.  The
# schema declares it `type: boolean`, but every other boolean arg in
# kanban_tools.py is run through `_parse_bool_arg` (which coerces the string
# forms "true"/"false"/"1"/"0"/"yes"/"no" and rejects garbage with a structured
# error) — schema type alone is not treated as a sufficient handler-layer guard.
# `skip_merge` must follow the same convention: a stray "false" string must NOT
# silently skip the merger on the irreversible approve path.
# ---------------------------------------------------------------------------


class TestApproveSkipMergeArgParsing:
    def test_skip_merge_string_false_does_not_skip(self, kanban_home, monkeypatch):
        """`{"skip_merge": "false"}` must route to `merging` (NOT skip the
        merger). A naive `bool("false")` is truthy and would wrongly skip."""
        from tools import kanban_tools as kt

        # Don't actually fork a merger subprocess.
        monkeypatch.setattr(kb, "_default_spawn", lambda *a, **k: None)
        monkeypatch.setattr(kb, "resolve_workspace", lambda *a, **k: "/tmp/x")
        monkeypatch.setattr(kb, "set_workspace_path", lambda *a, **k: None)

        with kb.connect() as conn:
            tid = _make_human_review_task(conn)

        out = kt._handle_approve({"task_id": tid, "skip_merge": "false"})
        d = json.loads(out)
        assert "error" not in d, d
        assert d["status"] == "merge_triggered"
        assert d["card_status"] == "merging"

        with kb.connect() as conn:
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.status == "merging"
            assert task.skip_merge is False

    def test_skip_merge_string_true_skips(self, kanban_home, monkeypatch):
        """`{"skip_merge": "true"}` must skip the merger → straight to done."""
        from tools import kanban_tools as kt

        spawned = []
        monkeypatch.setattr(
            kb, "_default_spawn", lambda *a, **k: spawned.append(a)
        )

        with kb.connect() as conn:
            tid = _make_human_review_task(conn)

        out = kt._handle_approve({"task_id": tid, "skip_merge": "true"})
        d = json.loads(out)
        assert "error" not in d, d
        assert d["status"] == "done"
        assert spawned == []  # no merger spawned

        with kb.connect() as conn:
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.status == "done"

    def test_skip_merge_malformed_returns_structured_error(
        self, kanban_home, monkeypatch
    ):
        """A non-boolean `skip_merge` returns a structured tool_error and leaves
        the task untouched in human_review (no approve, no merger)."""
        from tools import kanban_tools as kt

        spawned = []
        monkeypatch.setattr(
            kb, "_default_spawn", lambda *a, **k: spawned.append(a)
        )

        with kb.connect() as conn:
            tid = _make_human_review_task(conn)

        out = kt._handle_approve({"task_id": tid, "skip_merge": "maybe"})
        d = json.loads(out)
        assert "error" in d
        assert "skip_merge" in d["error"]
        assert spawned == []

        with kb.connect() as conn:
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.status == "human_review"

    def test_skip_merge_omitted_defaults_to_merging(self, kanban_home, monkeypatch):
        """Omitting `skip_merge` preserves the default merging-with-visibility
        path (parity with the no-arg/CLI behavior)."""
        from tools import kanban_tools as kt

        monkeypatch.setattr(kb, "_default_spawn", lambda *a, **k: None)
        monkeypatch.setattr(kb, "resolve_workspace", lambda *a, **k: "/tmp/x")
        monkeypatch.setattr(kb, "set_workspace_path", lambda *a, **k: None)

        with kb.connect() as conn:
            tid = _make_human_review_task(conn)

        out = kt._handle_approve({"task_id": tid})
        d = json.loads(out)
        assert "error" not in d, d
        assert d["card_status"] == "merging"

        with kb.connect() as conn:
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.status == "merging"

    def test_skip_merge_on_non_human_review_card_fails_and_does_not_persist(
        self, kanban_home, monkeypatch
    ):
        """`--skip-merge` approve on a card NOT in human_review must fail AND
        leave skip_merge unset.

        Regression for the existence-gated (not status-gated) write: the flag
        used to be persisted before approve_task ran, so a skip_merge=true
        approve on a `ready`/`running` card left a stray skip_merge=True on the
        card even though the approve failed. The flag must never persist when
        the paired approve does not go through.
        """
        from tools import kanban_tools as kt

        # A merger spawn must never happen on this path; trip the test if it does.
        monkeypatch.setattr(
            kb,
            "_default_spawn",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")),
        )

        with kb.connect() as conn:
            # Fresh task — status 'ready'/'todo', NOT human_review.
            tid = kb.create_task(conn, title="x", assignee="worker")
            _pre = kb.get_task(conn, tid)
            assert _pre is not None and _pre.skip_merge is False

        out = kt._handle_approve({"task_id": tid, "skip_merge": "true"})
        d = json.loads(out)
        assert "error" in d, d

        with kb.connect() as conn:
            task = kb.get_task(conn, tid)
        assert task is not None
        # Approve did not go through → card unchanged, flag NOT persisted.
        assert task.status != "human_review"
        assert task.status != "merging"
        assert task.status != "done"
        assert task.skip_merge is False

    def test_cli_skip_merge_on_non_human_review_card_fails_and_does_not_persist(
        self, kanban_home, monkeypatch
    ):
        """CLI `approve --skip-merge` on a non-human_review card mirrors the
        tool-layer behavior: returns non-zero AND leaves skip_merge unset."""
        import argparse

        from hermes_cli import kanban as kb_cli

        monkeypatch.setattr(
            kb,
            "_default_spawn",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")),
        )

        with kb.connect() as conn:
            tid = kb.create_task(conn, title="x", assignee="worker")
            _pre = kb.get_task(conn, tid)
            assert _pre is not None and _pre.skip_merge is False

        ns = argparse.Namespace(
            task_id=tid, reason=None, board=None, skip_merge=True,
        )
        rc = kb_cli._cmd_approve(ns)
        assert rc == 1

        with kb.connect() as conn:
            task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status != "human_review"
        assert task.status != "merging"
        assert task.status != "done"
        assert task.skip_merge is False
