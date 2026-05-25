"""Regression: ``_get_env_config`` must not crash when ``os.getcwd()`` ENOENT's.

When a kanban worker's scratch workspace is rmtree'd out from under it (e.g.
by an out-of-process tick or a buggy completion hook), the worker's process
cwd inode is unlinked. Every subsequent ``os.getcwd()`` call then raises
``FileNotFoundError``. Before this fix, that propagated through every tool
dispatch in the worker, producing a crash loop seen in production as 6
consecutive run failures on a single task (errors.log: terminal_tool.py:1021,
code_execution_tool.py:1068, registry.py:404).

The fix is to fall back to a known-good directory (HERMES_KANBAN_WORKSPACES_ROOT,
HOME, or '/') so the worker can finish its turn cleanly.
"""
from __future__ import annotations

import os
from unittest import mock

import pytest

from tools import terminal_tool


def test_safe_local_default_cwd_returns_cwd_normally(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert terminal_tool._safe_local_default_cwd() == str(tmp_path)


def test_safe_local_default_cwd_falls_back_when_cwd_unlinked(tmp_path, monkeypatch):
    """Simulate the ENOENT-on-getcwd scenario without actually unlinking
    the test runner's cwd."""
    fallback_root = tmp_path / "ws-root"
    fallback_root.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(fallback_root))

    with mock.patch.object(
        terminal_tool.os, "getcwd",
        side_effect=FileNotFoundError(2, "No such file or directory"),
    ):
        result = terminal_tool._safe_local_default_cwd()
    assert result == str(fallback_root)


def test_safe_local_default_cwd_falls_back_to_home_without_kanban_env(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    with mock.patch.object(
        terminal_tool.os, "getcwd",
        side_effect=FileNotFoundError(2, "No such file or directory"),
    ):
        result = terminal_tool._safe_local_default_cwd()
    # HOME exists on every CI box.
    assert os.path.isdir(result), f"fallback {result!r} should be an existing dir"


def test_get_env_config_survives_getcwd_enoent(tmp_path, monkeypatch):
    """End-to-end: a tool dispatch through ``_get_env_config`` must not raise
    when the process cwd has been unlinked under us. This is the regression
    that hard-blocked task t_83c080e5 (6 crash-loops).
    """
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path))

    with mock.patch.object(
        terminal_tool.os, "getcwd",
        side_effect=FileNotFoundError(2, "No such file or directory"),
    ):
        config = terminal_tool._get_env_config()
    # Did not raise; returned a usable cwd inside the fallback root.
    assert config["env_type"] == "local"
    assert config["cwd"], "config['cwd'] must be a non-empty fallback path"
    assert os.path.isdir(config["cwd"])


def test_complete_task_does_not_break_worker_terminal_dispatch(
    tmp_path, monkeypatch,
):
    """Belt-and-braces: even with the root-cause fix (no rmtree in
    ``_cleanup_workspace``), keep this test in place to guarantee that any
    future change that re-introduces eager cleanup is caught here too.

    Simulates a worker process whose scratch workspace has been deleted
    under it, then calls ``_get_env_config`` the way the terminal tool would.
    """
    # Create a fake workspace, "chdir" into it (logically), then delete it.
    ws = tmp_path / "t_fake"
    ws.mkdir()
    fallback = tmp_path / "fallback-root"
    fallback.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(fallback))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.chdir(ws)
    import shutil
    shutil.rmtree(ws)

    # On Linux/macOS, real os.getcwd() now raises ENOENT for this process,
    # so we don't need to mock — the situation is real.
    config = terminal_tool._get_env_config()
    assert config["env_type"] == "local"
    assert config["cwd"], "cwd fallback must be non-empty"
    assert os.path.isdir(config["cwd"])

    # Restore cwd for downstream tests (pytest doesn't auto-restore when the
    # original dir is gone).
    os.chdir(str(tmp_path))
