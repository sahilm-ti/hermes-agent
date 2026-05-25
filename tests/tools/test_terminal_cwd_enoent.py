"""Regression test: _get_env_config must survive a deleted cwd.

Observed in kanban-worker spawns whose scratch workspace directory was
cleaned mid-run: every call into ``_get_env_config`` raised
``FileNotFoundError`` from ``os.getcwd()`` and crashed the entire worker
within 60 seconds (6 consecutive crashes on task t_83c080e5, 2026-05-25).

The fix wraps ``os.getcwd()`` in a try/except in:
- tools/terminal_tool.py::_get_env_config (local backend default_cwd)
- tools/code_execution_tool.py::_resolve_project_cwd

This test exercises both paths.
"""
import os
import tempfile

import pytest


def _delete_cwd():
    """Put the test process in a cwd that has been ``rmdir``'d."""
    d = tempfile.mkdtemp(prefix="hermes-cwd-enoent-")
    os.chdir(d)
    os.rmdir(d)
    # Sanity check
    with pytest.raises((FileNotFoundError, OSError)):
        os.getcwd()


def test_get_env_config_survives_deleted_cwd(monkeypatch):
    """_get_env_config must not raise when cwd has been deleted."""
    from tools.terminal_tool import _get_env_config

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("TERMINAL_CWD", raising=False)

    original_cwd = os.path.expanduser("~")
    try:
        _delete_cwd()
        cfg = _get_env_config()
    finally:
        # Restore a sane cwd so subsequent tests don't inherit the broken one
        os.chdir(original_cwd)

    # The fix falls back to $HOME (or /tmp if HOME is empty); either is fine
    assert cfg["env_type"] == "local"
    assert cfg.get("cwd") not in (None, "")
    # Most importantly: we didn't raise.


def test_resolve_project_cwd_survives_deleted_cwd(monkeypatch):
    """_resolve_child_cwd must fall back to staging_dir when cwd is gone."""
    from tools.code_execution_tool import _resolve_child_cwd

    monkeypatch.delenv("TERMINAL_CWD", raising=False)

    original_cwd = os.path.expanduser("~")
    staging = tempfile.mkdtemp(prefix="hermes-staging-")
    try:
        _delete_cwd()
        result = _resolve_child_cwd(mode="project", staging_dir=staging)
    finally:
        os.chdir(original_cwd)
        try:
            os.rmdir(staging)
        except OSError:
            pass

    # Should have fallen back to staging since cwd was unreadable
    assert result == staging
