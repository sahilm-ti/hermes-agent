"""
Tests for hermes_cli.fork_tracking — the track-fork / merge-upstream contract.

Covers:
- detect_fork_tracking: inverted topology (origin=official, myfork=fork) → config
- detect_fork_tracking: canonical topology (origin=fork) → None
- detect_fork_tracking: fallback to first non-official non-origin remote
- branch_move_blocked: running worker / mid-merge / dirty → reason; else None
- any_kanban_task_running: running task present / absent / DB missing / unreadable
- merge_upstream_into_fork: clean merge → merged + pushed
- merge_upstream_into_fork: conflict → merge --abort + alert, no reset, stays on fork
- merge_upstream_into_fork: blocked when a worker is running (no branch move)
- merge_upstream_into_fork: never issues `reset --hard origin/...`
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_cli.fork_tracking import (
    ForkTrackingConfig,
    MergeResult,
    any_kanban_task_running,
    branch_move_blocked,
    detect_fork_tracking,
    merge_upstream_into_fork,
)

OFFICIAL = "git@github.com:NousResearch/hermes-agent.git"
FORK = "git@github.com:sahilm-ti/hermes-agent.git"
GIT = ["git"]
CWD = Path("/fake/checkout")


def _cp(returncode=0, stdout="", stderr=""):
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


class TestDetectForkTracking(unittest.TestCase):
    def test_inverted_topology_detected(self):
        """origin=official + myfork=fork → ForkTrackingConfig(origin, myfork)."""

        def fake_run_git(git_cmd, cwd, *args, **kwargs):
            if args[:2] == ("remote", "get-url"):
                remote = args[2]
                if remote == "origin":
                    return _cp(0, OFFICIAL)
                if remote == "myfork":
                    return _cp(0, FORK)
                return _cp(1, "")
            if args[:1] == ("remote",):
                return _cp(0, "origin\nmyfork\n")
            return _cp(1, "")

        with patch("hermes_cli.fork_tracking._run_git", side_effect=fake_run_git):
            cfg = detect_fork_tracking(GIT, CWD)
        self.assertIsNotNone(cfg)
        assert cfg is not None
        self.assertEqual(cfg.upstream_remote, "origin")
        self.assertEqual(cfg.fork_remote, "myfork")
        self.assertEqual(cfg.upstream_ref, "origin/main")
        self.assertEqual(cfg.fork_ref, "myfork/main")

    def test_canonical_topology_returns_none(self):
        """origin=fork (canonical) → None (use historical reset path)."""

        def fake_run_git(git_cmd, cwd, *args, **kwargs):
            if args[:2] == ("remote", "get-url") and args[2] == "origin":
                return _cp(0, FORK)
            if args[:1] == ("remote",):
                return _cp(0, "origin\nupstream\n")
            return _cp(1, "")

        with patch("hermes_cli.fork_tracking._run_git", side_effect=fake_run_git):
            cfg = detect_fork_tracking(GIT, CWD)
        self.assertIsNone(cfg)

    def test_fallback_to_first_nonofficial_remote(self):
        """When `myfork` is absent, use the first non-origin non-official remote."""

        def fake_run_git(git_cmd, cwd, *args, **kwargs):
            if args[:2] == ("remote", "get-url"):
                remote = args[2]
                if remote == "origin":
                    return _cp(0, OFFICIAL)
                if remote == "personal":
                    return _cp(0, FORK)
                return _cp(1, "")
            if args[:1] == ("remote",):
                return _cp(0, "origin\npersonal\n")
            return _cp(1, "")

        with patch("hermes_cli.fork_tracking._run_git", side_effect=fake_run_git):
            cfg = detect_fork_tracking(GIT, CWD)
        self.assertIsNotNone(cfg)
        assert cfg is not None
        self.assertEqual(cfg.fork_remote, "personal")

    def test_official_origin_but_no_fork_remote_returns_none(self):
        """origin=official but every other remote is also official → None."""

        def fake_run_git(git_cmd, cwd, *args, **kwargs):
            if args[:2] == ("remote", "get-url"):
                return _cp(0, OFFICIAL)
            if args[:1] == ("remote",):
                return _cp(0, "origin\nupstream\n")
            return _cp(1, "")

        with patch("hermes_cli.fork_tracking._run_git", side_effect=fake_run_git):
            cfg = detect_fork_tracking(GIT, CWD)
        self.assertIsNone(cfg)


class TestAnyKanbanTaskRunning(unittest.TestCase):
    def test_returns_false_when_db_missing(self):
        with patch(
            "hermes_cli.fork_tracking._kanban_db_path",
            return_value=Path("/nonexistent/kanban.db"),
        ):
            self.assertFalse(any_kanban_task_running())

    def test_returns_true_when_db_unreadable(self):
        """Unreadable DB → conservative True (treat as 'task running')."""
        fake_path = MagicMock(spec=Path)
        fake_path.exists.return_value = True
        with (
            patch("hermes_cli.fork_tracking._kanban_db_path", return_value=fake_path),
            patch("sqlite3.connect", side_effect=OSError("locked")),
        ):
            self.assertTrue(any_kanban_task_running())

    def test_running_task_detected(self):
        """Live in-memory DB with a running task → True; excludes self."""
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "kanban.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE task (id TEXT, status TEXT)")
            conn.execute("INSERT INTO task VALUES ('t_self', 'running')")
            conn.execute("INSERT INTO task VALUES ('t_other', 'running')")
            conn.execute("INSERT INTO task VALUES ('t_done', 'done')")
            conn.commit()
            conn.close()

            with patch("hermes_cli.fork_tracking._kanban_db_path", return_value=db):
                # Another task is running (excluding self).
                self.assertTrue(any_kanban_task_running(exclude_task_id="t_self"))
                # Excluding the other running task still leaves t_self running.
                self.assertTrue(any_kanban_task_running(exclude_task_id="t_other"))

    def test_only_self_running_is_not_blocking(self):
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "kanban.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE task (id TEXT, status TEXT)")
            conn.execute("INSERT INTO task VALUES ('t_self', 'running')")
            conn.execute("INSERT INTO task VALUES ('t_done', 'done')")
            conn.commit()
            conn.close()

            with patch("hermes_cli.fork_tracking._kanban_db_path", return_value=db):
                self.assertFalse(any_kanban_task_running(exclude_task_id="t_self"))


class TestBranchMoveBlocked(unittest.TestCase):
    def test_blocked_when_task_running(self):
        with patch(
            "hermes_cli.fork_tracking.any_kanban_task_running", return_value=True
        ):
            reason = branch_move_blocked(GIT, CWD)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("kanban task", reason)

    def test_blocked_when_mid_merge(self):
        with (
            patch(
                "hermes_cli.fork_tracking.any_kanban_task_running", return_value=False
            ),
            patch("hermes_cli.fork_tracking.is_mid_operation", return_value=True),
        ):
            reason = branch_move_blocked(GIT, CWD)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("mid-merge", reason)

    def test_blocked_when_dirty(self):
        with (
            patch(
                "hermes_cli.fork_tracking.any_kanban_task_running", return_value=False
            ),
            patch("hermes_cli.fork_tracking.is_mid_operation", return_value=False),
            patch("hermes_cli.fork_tracking.is_clean", return_value=False),
        ):
            reason = branch_move_blocked(GIT, CWD)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("dirty", reason)

    def test_not_blocked_when_all_clear(self):
        with (
            patch(
                "hermes_cli.fork_tracking.any_kanban_task_running", return_value=False
            ),
            patch("hermes_cli.fork_tracking.is_mid_operation", return_value=False),
            patch("hermes_cli.fork_tracking.is_clean", return_value=True),
        ):
            self.assertIsNone(branch_move_blocked(GIT, CWD))


class TestMergeUpstreamIntoFork(unittest.TestCase):
    CFG = ForkTrackingConfig(upstream_remote="origin", fork_remote="myfork")

    def _unblocked(self):
        """Context: branch_move_blocked returns None."""
        return patch("hermes_cli.fork_tracking.branch_move_blocked", return_value=None)

    def test_blocked_when_worker_running_does_not_move_branch(self):
        calls = []

        def fake_run_git(git_cmd, cwd, *args, **kwargs):
            calls.append(args)
            return _cp(0, "")

        with (
            patch(
                "hermes_cli.fork_tracking.branch_move_blocked",
                return_value="a kanban task is running",
            ),
            patch("hermes_cli.fork_tracking._run_git", side_effect=fake_run_git),
        ):
            res = merge_upstream_into_fork(GIT, CWD, self.CFG)
        self.assertEqual(res.status, "blocked")
        # No fetch / merge / push / reset issued.
        for args in calls:
            self.assertNotIn("fetch", args)
            self.assertNotIn("merge", args)
            self.assertNotIn("push", args)
            self.assertNotIn("reset", args)

    def test_clean_merge_commits_and_pushes_to_fork(self):
        seq = []

        def fake_run_git(git_cmd, cwd, *args, **kwargs):
            seq.append(args)
            if args[:1] == ("fetch",):
                return _cp(0, "")
            if args == ("rev-parse", "HEAD"):
                return _cp(0, "abc123fork\n")
            if args[:2] == ("rev-list", "--count"):
                rng = args[2]
                if rng.startswith("HEAD..myfork"):
                    return _cp(0, "0\n")  # fork did not advance
                if rng.startswith("HEAD..origin"):
                    return _cp(0, "5\n")  # 5 upstream commits to merge
                if rng.startswith("myfork/main..HEAD"):
                    return _cp(0, "1\n")
                return _cp(0, "0\n")
            if args[0] == "merge" and "--no-ff" in args:
                return _cp(0, "Merge made")
            if args[0] == "rev-parse" and args[1] == "myfork/main":
                return _cp(0, "deadbeeffork\n")
            if args[0] == "push":
                return _cp(0, "pushed")
            return _cp(0, "")

        with (
            self._unblocked(),
            patch("hermes_cli.fork_tracking._run_git", side_effect=fake_run_git),
        ):
            res = merge_upstream_into_fork(GIT, CWD, self.CFG)

        self.assertEqual(res.status, "merged")
        self.assertTrue(res.pushed)
        # A push to the FORK with --force-with-lease happened; never to origin.
        push_calls = [a for a in seq if a[0] == "push"]
        self.assertTrue(push_calls)
        for pc in push_calls:
            self.assertIn("myfork", pc)
            self.assertNotIn("origin", pc)
            self.assertTrue(any(str(x).startswith("--force-with-lease") for x in pc))
        # NEVER a reset to origin.
        self.assertFalse(
            any(a[0] == "reset" for a in seq), "merge path must never reset"
        )

    def test_conflict_aborts_and_alerts_no_reset(self):
        seq = []
        alerts = []

        def fake_run_git(git_cmd, cwd, *args, **kwargs):
            seq.append(args)
            if args[:1] == ("fetch",):
                return _cp(0, "")
            if args == ("rev-parse", "HEAD"):
                return _cp(0, "forkSHA0\n")
            if args[:2] == ("rev-list", "--count"):
                rng = args[2]
                if rng.startswith("HEAD..myfork"):
                    return _cp(0, "0\n")
                if rng.startswith("HEAD..origin"):
                    return _cp(0, "3\n")
                return _cp(0, "0\n")
            if args[0] == "merge" and "--no-ff" in args:
                return _cp(1, "CONFLICT (content): merge conflict in run.py")
            if args == ("merge", "--abort"):
                return _cp(0, "")
            return _cp(0, "")

        with (
            self._unblocked(),
            patch("hermes_cli.fork_tracking._run_git", side_effect=fake_run_git),
        ):
            res = merge_upstream_into_fork(
                GIT, CWD, self.CFG, alert=lambda m: alerts.append(m)
            )

        self.assertEqual(res.status, "conflict")
        # merge --abort was issued.
        self.assertIn(("merge", "--abort"), seq)
        # operator was alerted.
        self.assertTrue(alerts)
        self.assertIn("conflict", alerts[0].lower())
        # NEVER reset to origin, NEVER pushed.
        self.assertFalse(any(a[0] == "reset" for a in seq))
        self.assertFalse(any(a[0] == "push" for a in seq))

    def test_up_to_date_when_no_upstream_commits(self):
        def fake_run_git(git_cmd, cwd, *args, **kwargs):
            if args[:1] == ("fetch",):
                return _cp(0, "")
            if args == ("rev-parse", "HEAD"):
                return _cp(0, "forkSHA\n")
            if args[:2] == ("rev-list", "--count"):
                rng = args[2]
                if rng.startswith("HEAD..myfork"):
                    return _cp(0, "0\n")
                if rng.startswith("HEAD..origin"):
                    return _cp(0, "0\n")  # already contains all upstream
                if rng.startswith("myfork/main..HEAD"):
                    return _cp(0, "0\n")  # fork has nothing to push
                return _cp(0, "0\n")
            return _cp(0, "")

        with (
            self._unblocked(),
            patch("hermes_cli.fork_tracking._run_git", side_effect=fake_run_git),
        ):
            res = merge_upstream_into_fork(GIT, CWD, self.CFG)
        self.assertEqual(res.status, "up_to_date")
        self.assertFalse(res.pushed)


if __name__ == "__main__":
    unittest.main()
