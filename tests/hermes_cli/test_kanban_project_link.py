"""Kanban <-> Projects integration: project-linked tasks get a deterministic
worktree path + branch instead of the random ``wt/<task-id>`` fallback."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    return home


@pytest.fixture
def kanban_conn(kanban_home):
    c = kb.connect()
    try:
        yield c
    finally:
        c.close()


def _make_project(*, name: str = "Web App", repo: str = "/tmp/webapp"):
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name=name, folders=[repo])
        return pdb.get_project(pc, pid)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Kanban Test")
    _git(path, "config", "user.email", "kanban-test@example.com")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")


def test_project_linked_task_gets_deterministic_worktree_and_branch(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(kanban_conn, title="Add login", project_id=proj.slug)
    task = kb.get_task(kanban_conn, tid)

    assert task is not None
    assert task.project_id == proj.id
    assert task.workspace_kind == "worktree"
    # Worktree dir anchored under the project's primary repo, keyed on task id.
    assert task.workspace_path == os.path.join(proj.primary_path, ".worktrees", tid)
    # Deterministic branch: <slug>/<task-id>-<title-slug>. NOT a random wt/...
    assert task.branch_name == f"{proj.slug}/{tid}-add-login"
    assert task.branch_name is not None
    assert not task.branch_name.startswith("wt/")


def test_explicit_branch_overrides_project_default(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(
        kanban_conn,
        title="x",
        project_id=proj.slug,
        workspace_kind="worktree",
        branch_name="feature/custom",
    )
    task = kb.get_task(kanban_conn, tid)
    assert task is not None
    assert task.branch_name == "feature/custom"


def test_unlinked_task_unchanged(kanban_conn):
    tid = kb.create_task(kanban_conn, title="plain")
    task = kb.get_task(kanban_conn, tid)

    assert task is not None
    assert task.project_id is None
    assert task.workspace_kind == "scratch"
    # No branch is persisted — the worker still owns the wt/<id> fallback for
    # genuinely ad-hoc worktree tasks, but unlinked scratch tasks have none.
    assert task.branch_name is None


def test_dispatch_materializes_project_worktree_before_spawn_preflight(
    kanban_conn, tmp_path, all_assignees_spawnable
):
    repo = tmp_path / "project-repo"
    _init_git_repo(repo)
    proj = _make_project(repo=str(repo))
    task_id = kb.create_task(
        kanban_conn,
        title="materialize me",
        assignee="alice",
        project_id=proj.slug,
    )
    target = (repo / ".worktrees" / task_id).resolve()

    spawned: list[str] = []

    def preflight_spawn(task, workspace, board=None):
        spawned.append(workspace)
        assert Path(workspace).resolve() == target
        assert Path(workspace).is_dir()
        repo_common = _git(
            repo, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout.strip()
        workspace_common = _git(
            Path(workspace), "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout.strip()
        assert Path(workspace_common).resolve() == Path(repo_common).resolve()
        return 4242

    result = kb.dispatch_once(kanban_conn, spawn_fn=preflight_spawn)
    task = kb.get_task(kanban_conn, task_id)

    assert result.spawned == [(task_id, "alice", str(target))]
    assert spawned == [str(target)]
    assert task is not None
    assert task.workspace_path == str(target)
    assert task.status == "running"


def test_dispatch_project_worktree_retry_is_idempotent(
    kanban_conn, tmp_path, all_assignees_spawnable
):
    repo = tmp_path / "retry-repo"
    _init_git_repo(repo)
    proj = _make_project(name="Retry Repo", repo=str(repo))
    task_id = kb.create_task(
        kanban_conn,
        title="retry path",
        assignee="alice",
        project_id=proj.slug,
    )
    target = (repo / ".worktrees" / task_id).resolve()
    expected_branch = f"{proj.slug}/{task_id}-retry-path"
    attempts = {"count": 0}

    def flaky_spawn(task, workspace, board=None):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("synthetic worker preflight failure")
        return 5150

    first = kb.dispatch_once(kanban_conn, spawn_fn=flaky_spawn)
    task_after_first = kb.get_task(kanban_conn, task_id)

    assert first.spawned == []
    assert target.is_dir()
    assert task_after_first is not None
    assert task_after_first.status == "ready"
    assert task_after_first.consecutive_failures == 1

    second = kb.dispatch_once(kanban_conn, spawn_fn=flaky_spawn)
    task_after_second = kb.get_task(kanban_conn, task_id)
    listed = _git(repo, "worktree", "list", "--porcelain").stdout.splitlines()

    assert attempts["count"] == 2
    assert second.spawned == [(task_id, "alice", str(target))]
    assert sum(1 for line in listed if line == f"worktree {target}") == 1
    assert _git(repo, "rev-parse", "--verify", f"refs/heads/{expected_branch}").stdout.strip()
    assert task_after_second is not None
    assert task_after_second.status == "running"


def test_dispatch_project_worktree_invalid_root_reports_precise_retryable_error(
    kanban_conn, tmp_path, all_assignees_spawnable
):
    missing_root = tmp_path / "missing-project-repo"
    proj = _make_project(name="Broken Project", repo=str(missing_root))
    task_id = kb.create_task(
        kanban_conn,
        title="broken root",
        assignee="alice",
        project_id=proj.slug,
    )
    expected_destination = str((missing_root / ".worktrees" / task_id).resolve(strict=False))
    spawn_called = {"called": False}

    def should_not_spawn(task, workspace, board=None):
        spawn_called["called"] = True
        return 1

    result = kb.dispatch_once(kanban_conn, spawn_fn=should_not_spawn)
    task = kb.get_task(kanban_conn, task_id)
    events = [e for e in kb.list_events(kanban_conn, task_id) if e.kind == "spawn_failed"]
    assert events, "expected a spawn_failed event for invalid project roots"
    payload = events[-1].payload

    assert spawn_called["called"] is False
    assert result.spawned == []
    assert task is not None
    assert payload is not None
    reason = payload.get("error", "")
    assert task.status == "ready"
    assert task.consecutive_failures == 1
    assert "project-linked worktree destination" in reason
    assert str(missing_root) in reason
    assert expected_destination in reason


def test_dispatch_non_project_unresolved_worktree_still_rejected(
    kanban_conn, tmp_path, all_assignees_spawnable
):
    unresolved = tmp_path / "not-a-repo" / ".worktrees" / "placeholder"
    task_id = kb.create_task(
        kanban_conn,
        title="plain unresolved worktree",
        assignee="alice",
        workspace_kind="worktree",
        workspace_path=str(unresolved),
    )
    spawn_called = {"called": False}

    def should_not_spawn(task, workspace, board=None):
        spawn_called["called"] = True
        return 2

    kb.dispatch_once(kanban_conn, spawn_fn=should_not_spawn)
    task = kb.get_task(kanban_conn, task_id)
    events = [e for e in kb.list_events(kanban_conn, task_id) if e.kind == "spawn_failed"]
    assert events, "expected spawn_failed event for unresolved non-project worktree"
    payload = events[-1].payload

    assert spawn_called["called"] is False
    assert task is not None
    assert payload is not None
    reason = payload.get("error", "")
    assert task.status == "ready"
    assert "does not point at a git repo root" in reason


def test_dispatch_scratch_workspace_behavior_unchanged(
    kanban_conn, all_assignees_spawnable
):
    task_id = kb.create_task(kanban_conn, title="scratch dispatch", assignee="alice")
    seen_workspace: list[str] = []

    def capture_spawn(task, workspace, board=None):
        seen_workspace.append(workspace)
        assert Path(workspace).is_dir()
        return 9090

    result = kb.dispatch_once(kanban_conn, spawn_fn=capture_spawn)
    task = kb.get_task(kanban_conn, task_id)

    assert result.spawned
    assert seen_workspace and Path(seen_workspace[0]).is_dir()
    assert task is not None
    assert task.workspace_kind == "scratch"
    assert task.status == "running"


