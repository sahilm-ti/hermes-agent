"""
Auto-pull watcher for the ~/.hermes config checkout.

``~/.hermes`` is a git checkout of the user's hermes-config repo.  When new
skills, SOULs, or config changes are merged via PR, the live checkout drifts
behind ``origin/main`` until someone manually ``git pull``.  This module
adds a background thread that fast-forwards the checkout once per hour when
the tree is clean and on ``main``.

Safety contract
---------------
- Only fast-forwards (``git pull --ff-only``).  Never force-pushes, never
  rebases, never touches an ahead-of-origin or diverged tree.
- Skips silently when the tree is dirty (uncommitted local edits are
  preserved).
- Skips silently when the current branch is not ``main``.
- All subprocess calls are non-blocking relative to the event loop — the
  watcher runs in a daemon thread.

Configuration
-------------
config.yaml:
    gateway:
        hermes_home_auto_pull: true   # default: true

Environment override (wins over config):
    HERMES_GATEWAY_NO_AUTO_PULL=1    # disable auto-pull
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Interval between pull checks (seconds).  Once per hour is intentionally
# conservative — this is a "drift catch-up" tool, not a live-sync mechanism.
_CHECK_INTERVAL_SECS = 3600

# Timeout for each git subprocess call.
_GIT_TIMEOUT_SECS = 30


def _is_disabled_by_env() -> bool:
    """Return True when the env-var opt-out is active."""
    val = os.environ.get("HERMES_GATEWAY_NO_AUTO_PULL", "").strip()
    return val not in ("", "0", "false", "False", "no", "No")


def _is_disabled_by_config(cfg: Optional[Dict]) -> bool:
    """Return True when config.yaml opts out."""
    if cfg is None:
        return False
    try:
        from hermes_cli.config import cfg_get

        value = cfg_get(cfg, "gateway", "hermes_home_auto_pull", default=True)
        if isinstance(value, bool):
            return not value
        if isinstance(value, str):
            return value.strip().lower() in ("false", "0", "no", "off")
        return not bool(value)
    except Exception:
        return False  # config unreadable — don't disable


def _run_git(hermes_home: Path, *args: str) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    """Run a git command in *hermes_home*.  Returns the CompletedProcess."""
    cmd = ["git", "-C", str(hermes_home), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECS,
    )


def _current_branch(hermes_home: Path) -> str:
    """Return the current branch name, or '' on failure."""
    result = _run_git(hermes_home, "rev-parse", "--abbrev-ref", "HEAD")
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _is_clean(hermes_home: Path) -> bool:
    """Return True when there are no staged, unstaged, or untracked changes."""
    result = _run_git(hermes_home, "status", "--porcelain")
    if result.returncode != 0:
        return False
    return result.stdout.strip() == ""


def _commits_behind(hermes_home: Path) -> int:
    """
    Return how many commits HEAD is behind origin/main.

    Runs ``git fetch`` first (quiet, so a slow network doesn't cause noise),
    then counts commits reachable from origin/main but not from HEAD.
    Returns 0 if the fetch fails (conservative — don't pull if we can't
    verify remote state).
    """
    fetch = _run_git(hermes_home, "fetch", "origin", "--quiet")
    if fetch.returncode != 0:
        logger.debug(
            "hermes-home puller: git fetch failed (%s); skipping this check",
            fetch.stderr.strip(),
        )
        return 0

    rev_list = _run_git(
        hermes_home, "rev-list", "--count", "HEAD..origin/main"
    )
    if rev_list.returncode != 0:
        return 0
    try:
        return int(rev_list.stdout.strip())
    except ValueError:
        return 0


def _fast_forward(hermes_home: Path) -> bool:
    """
    Attempt a fast-forward pull from origin/main.

    Returns True on success, False on failure.  Logs outcome at INFO
    (success) or WARNING (failure).
    """
    result = _run_git(
        hermes_home, "pull", "--ff-only", "origin", "main", "--quiet"
    )
    if result.returncode == 0:
        logger.info(
            "hermes-home puller: fast-forwarded ~/.hermes to origin/main"
        )
        return True
    logger.warning(
        "hermes-home puller: pull --ff-only failed (%s)",
        result.stderr.strip(),
    )
    return False


def _pull_once(hermes_home: Path) -> None:
    """
    Single pull attempt: check branch → check clean → count behind → pull.

    All failures are non-fatal: they log at DEBUG/WARNING and return.
    """
    try:
        branch = _current_branch(hermes_home)
        if branch != "main":
            logger.debug(
                "hermes-home puller: skipping pull — on branch %r (not main)",
                branch,
            )
            return

        if not _is_clean(hermes_home):
            logger.debug(
                "hermes-home puller: skipping pull — working tree is dirty"
            )
            return

        behind = _commits_behind(hermes_home)
        if behind == 0:
            logger.debug(
                "hermes-home puller: ~/.hermes is up-to-date with origin/main"
            )
            return

        logger.info(
            "hermes-home puller: ~/.hermes is %d commit(s) behind origin/main; pulling",
            behind,
        )
        _fast_forward(hermes_home)

    except subprocess.TimeoutExpired:
        logger.warning("hermes-home puller: git timed out; will retry next cycle")
    except Exception:
        logger.exception("hermes-home puller: unexpected error during pull check")


class HermesHomePuller:
    """
    Background daemon thread that periodically fast-forwards ~/.hermes.

    Lifecycle mirrors :class:`gateway.code_watcher.CodeWatcher` — construct,
    then call :meth:`start` to launch the thread, :meth:`stop` to join it.
    """

    def __init__(
        self,
        hermes_home: Path,
        interval: float = _CHECK_INTERVAL_SECS,
    ) -> None:
        self._hermes_home = hermes_home
        self._interval = interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="hermes-home-puller",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "hermes-home puller: started (interval=%ds, home=%s)",
            int(self._interval),
            self._hermes_home,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # Run once at startup (after a short warm-up delay) so the first
        # check happens quickly — avoids waiting a full hour on gateway boot.
        time.sleep(10)
        _pull_once(self._hermes_home)

        while not self._stop_event.wait(timeout=self._interval):
            _pull_once(self._hermes_home)


def start_hermes_home_puller(
    cfg: Optional[Dict] = None,
    hermes_home: Optional[Path] = None,
    interval: float = _CHECK_INTERVAL_SECS,
) -> Optional[HermesHomePuller]:
    """
    Create and start a :class:`HermesHomePuller` if auto-pull is enabled.

    Returns the puller instance (so the caller can stop it cleanly), or
    ``None`` when auto-pull is disabled.

    ``hermes_home`` defaults to the value of :func:`hermes_constants.get_hermes_home`.
    """
    if _is_disabled_by_env():
        logger.info("hermes-home puller: disabled via HERMES_GATEWAY_NO_AUTO_PULL env")
        return None

    if _is_disabled_by_config(cfg):
        logger.info("hermes-home puller: disabled via config gateway.hermes_home_auto_pull=false")
        return None

    if hermes_home is None:
        try:
            from hermes_constants import get_hermes_home

            hermes_home = get_hermes_home()
        except Exception:
            logger.warning(
                "hermes-home puller: cannot resolve hermes_home; disabled"
            )
            return None

    git_dir = hermes_home / ".git"
    if not git_dir.exists():
        logger.info(
            "hermes-home puller: %s has no .git directory; disabled (not a git checkout)",
            hermes_home,
        )
        return None

    puller = HermesHomePuller(hermes_home=hermes_home, interval=interval)
    puller.start()
    return puller
