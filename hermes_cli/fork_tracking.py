"""
Fork-tracking update contract for ``hermes update`` and the gateway puller.

Background
----------
Most Hermes checkouts follow the canonical convention: ``origin`` points at
the user's own fork and ``upstream`` (when present) points at
``NousResearch/hermes-agent``.  ``hermes update`` is built around that
assumption — it fast-forwards ``origin/main`` and, on divergence, *resets*
the local tree to match ``origin`` exactly.

Some operator checkouts invert this.  On those machines ``origin`` is the
**official upstream** (``NousResearch/hermes-agent``) and a *separate* remote
(by convention ``myfork``) is the fork that actually runs locally and carries
operator-only commits.  On such a checkout the canonical reset-to-``origin``
fallback is catastrophic: it discards every fork-only commit and replaces the
running tree with pure upstream.

This module implements the inverted contract:

1. The local checkout tracks the **fork** (``myfork/main``), not upstream.
2. Upstream is integrated by **merging** ``origin/main`` *into* the fork —
   never by resetting.
3. A clean merge is committed and pushed back to the fork
   (``git push <fork> main --force-with-lease``).  Upstream (``origin``) is
   never pushed to.
4. A conflicting merge is **aborted** (``git merge --abort``), the local tree
   is left on its last-good fork state, and the operator is alerted that an
   upstream merge needs manual resolution.  A reset is *never* used to
   resolve a conflict.

Safety
------
Branch-moving operations are gated on three conditions, all of which must
hold before any ff / merge / push:

- No kanban task is currently ``running`` (the update path and the gateway
  puller fire inside the same live checkout that kanban workers operate in;
  moving ``main`` mid-task wiped a worker's merge commit once — never again).
- The working tree is clean.
- The tree is not mid-merge / mid-rebase / mid-cherry-pick.

All public helpers are pure functions over a ``git_cmd`` prefix + ``cwd`` so
they can be unit-tested with mocked ``subprocess`` calls.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# The official upstream repository, in the URL spellings git may report.
# Kept in sync with hermes_cli.main.OFFICIAL_REPO_URLS; duplicated here so
# this module has no import cycle with main.py.
OFFICIAL_REPO_URLS = {
    "https://github.com/NousResearch/hermes-agent.git",
    "git@github.com:NousResearch/hermes-agent.git",
    "https://github.com/NousResearch/hermes-agent",
    "git@github.com:NousResearch/hermes-agent",
    "ssh://git@github.com/NousResearch/hermes-agent.git",
    "ssh://git@github.com/NousResearch/hermes-agent",
}

# Default remote name for the fork in an inverted-topology checkout.
DEFAULT_FORK_REMOTE = "myfork"

# Timeout for each git subprocess call (seconds).
_GIT_TIMEOUT_SECS = 120


def _normalize_repo_url(url: Optional[str]) -> str:
    """Lower-noise normalization for remote-URL comparison."""
    if not url:
        return ""
    normalized = url.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def _is_official_url(url: Optional[str]) -> bool:
    """True when *url* points at the official upstream repository."""
    normalized = _normalize_repo_url(url)
    if not normalized:
        return False
    return any(normalized == _normalize_repo_url(o) for o in OFFICIAL_REPO_URLS)


@dataclass(frozen=True)
class ForkTrackingConfig:
    """Resolved fork-tracking topology for a checkout.

    ``upstream_remote`` is the remote whose ``main`` we merge *in* (origin =
    NousResearch on this machine).  ``fork_remote`` is the remote we track and
    push the merge result to.
    """

    upstream_remote: str
    fork_remote: str
    branch: str = "main"

    @property
    def upstream_ref(self) -> str:
        return f"{self.upstream_remote}/{self.branch}"

    @property
    def fork_ref(self) -> str:
        return f"{self.fork_remote}/{self.branch}"


def _run_git(
    git_cmd: List[str], cwd: Path, *args: str, timeout: int = _GIT_TIMEOUT_SECS
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in *cwd* using the *git_cmd* prefix."""
    return subprocess.run(
        git_cmd + ["-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _remote_url(git_cmd: List[str], cwd: Path, remote: str) -> Optional[str]:
    result = _run_git(git_cmd, cwd, "remote", "get-url", remote, timeout=10)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _list_remotes(git_cmd: List[str], cwd: Path) -> List[str]:
    result = _run_git(git_cmd, cwd, "remote", timeout=10)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def detect_fork_tracking(
    git_cmd: List[str],
    cwd: Path,
    branch: str = "main",
    preferred_fork_remote: str = DEFAULT_FORK_REMOTE,
) -> Optional[ForkTrackingConfig]:
    """Detect an inverted-topology fork-tracking checkout.

    Returns a :class:`ForkTrackingConfig` when:

    - ``origin`` points at the official upstream repo, AND
    - a *separate* remote points at a non-official repo (the fork).

    The fork remote is ``preferred_fork_remote`` (``myfork``) when present and
    non-official; otherwise the first non-``origin`` remote with a non-official
    URL is used.  Returns ``None`` for a canonical checkout (origin = fork) so
    callers fall back to the historical behavior.
    """
    origin_url = _remote_url(git_cmd, cwd, "origin")
    if not _is_official_url(origin_url):
        # Canonical convention (origin = fork) or no origin — not our case.
        return None

    remotes = _list_remotes(git_cmd, cwd)

    # Prefer the conventional fork remote name when it's present and points at
    # a non-official repo.
    if preferred_fork_remote in remotes:
        fork_url = _remote_url(git_cmd, cwd, preferred_fork_remote)
        if fork_url and not _is_official_url(fork_url):
            return ForkTrackingConfig(
                upstream_remote="origin",
                fork_remote=preferred_fork_remote,
                branch=branch,
            )

    # Otherwise, any non-origin remote pointing at a non-official repo.
    for remote in remotes:
        if remote == "origin":
            continue
        url = _remote_url(git_cmd, cwd, remote)
        if url and not _is_official_url(url):
            return ForkTrackingConfig(
                upstream_remote="origin", fork_remote=remote, branch=branch
            )

    return None


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------


def is_clean(git_cmd: List[str], cwd: Path) -> bool:
    """True when there are no staged, unstaged, or untracked changes."""
    result = _run_git(git_cmd, cwd, "status", "--porcelain", timeout=15)
    if result.returncode != 0:
        return False
    return result.stdout.strip() == ""


def is_mid_operation(cwd: Path) -> bool:
    """True when the tree is mid-merge / mid-rebase / mid-cherry-pick.

    Inspects the well-known sentinel paths git drops into ``.git`` during an
    in-progress operation.  Worktree-aware: resolves the real gitdir.
    """
    git_dir = _resolve_git_dir(cwd)
    if git_dir is None:
        return False
    sentinels = (
        "MERGE_HEAD",
        "rebase-merge",
        "rebase-apply",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
    )
    return any((git_dir / name).exists() for name in sentinels)


def _resolve_git_dir(cwd: Path) -> Optional[Path]:
    """Resolve the actual ``.git`` directory for *cwd* (worktree-aware)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = (cwd / path).resolve()
    return path


def any_kanban_task_running(exclude_task_id: Optional[str] = None) -> bool:
    """True when any kanban task is currently ``running``.

    Reads the kanban SQLite DB directly (the dispatcher and workers both write
    it) so this works regardless of terminal backend.  ``exclude_task_id``
    omits the caller's own task (e.g. the worker that triggered the update).
    Falls back to the ``HERMES_KANBAN_TASK`` env when not given.

    Conservative: any error reading the DB returns ``True`` (treat unknown
    state as "a task might be running" and skip the branch-moving op).  When
    the DB simply doesn't exist, returns ``False`` (no kanban in play).
    """
    if exclude_task_id is None:
        exclude_task_id = os.environ.get("HERMES_KANBAN_TASK") or None

    db_path = _kanban_db_path()
    if db_path is None or not db_path.exists():
        return False

    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            # Tolerate either schema name (`task` vs `tasks`).
            table = _kanban_task_table(conn)
            if table is None:
                return False
            if exclude_task_id:
                cur = conn.execute(
                    f"SELECT COUNT(*) FROM {table} "
                    "WHERE status = 'running' AND id != ?",
                    (exclude_task_id,),
                )
            else:
                cur = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE status = 'running'"
                )
            row = cur.fetchone()
            return bool(row and row[0] > 0)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(
            "fork-tracking: could not read kanban DB (%s); "
            "treating as 'task running' and skipping branch move",
            exc,
        )
        return True


def _kanban_db_path() -> Optional[Path]:
    """Resolve the active kanban DB path."""
    env = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if env:
        return Path(env)
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "kanban.db"
    except Exception:
        return None


def _kanban_task_table(conn) -> Optional[str]:
    """Return the task table name (`task` or `tasks`), or None if absent."""
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name IN ('task', 'tasks')"
        )
        names = {row[0] for row in cur.fetchall()}
    except Exception:
        return None
    if "task" in names:
        return "task"
    if "tasks" in names:
        return "tasks"
    return None


def branch_move_blocked(
    git_cmd: List[str], cwd: Path, exclude_task_id: Optional[str] = None
) -> Optional[str]:
    """Return a human reason string when a branch-moving op must be skipped.

    Returns ``None`` when it is safe to ff / merge / move ``main``.
    """
    if any_kanban_task_running(exclude_task_id=exclude_task_id):
        return "a kanban task is running"
    if is_mid_operation(cwd):
        return "the tree is mid-merge/rebase/cherry-pick"
    if not is_clean(git_cmd, cwd):
        return "the working tree is dirty"
    return None


# ---------------------------------------------------------------------------
# The merge-upstream-into-fork contract
# ---------------------------------------------------------------------------


@dataclass
class MergeResult:
    """Outcome of :func:`merge_upstream_into_fork`."""

    status: str  # "merged" | "ff" | "up_to_date" | "conflict" | "blocked" | "error"
    detail: str = ""
    head_sha: str = ""
    pushed: bool = False


def _head_sha(git_cmd: List[str], cwd: Path) -> str:
    result = _run_git(git_cmd, cwd, "rev-parse", "HEAD", timeout=10)
    return result.stdout.strip() if result.returncode == 0 else ""


def _count_commits(git_cmd: List[str], cwd: Path, rev_range: str) -> int:
    """``git rev-list --count <rev_range>`` → int, or -1 on error."""
    result = _run_git(git_cmd, cwd, "rev-list", "--count", rev_range, timeout=30)
    if result.returncode != 0:
        return -1
    try:
        return int(result.stdout.strip())
    except ValueError:
        return -1


def merge_upstream_into_fork(
    git_cmd: List[str],
    cwd: Path,
    cfg: ForkTrackingConfig,
    *,
    push: bool = True,
    alert: Optional[Callable[[str], None]] = None,
    exclude_task_id: Optional[str] = None,
) -> MergeResult:
    """Execute the track-fork / merge-upstream contract.

    Steps (all gated on :func:`branch_move_blocked` returning ``None``):

    1. Fetch both remotes.
    2. Fast-forward local ``branch`` to ``fork/branch`` if the fork advanced.
    3. Merge ``upstream/branch`` into the fork (``--no-ff``).
       - Clean   → commit, optionally push to fork, return ``merged``/``ff``.
       - Conflict→ ``merge --abort``, alert, leave local on last-good fork
                   state, return ``conflict``.  **Never resets.**

    *alert* is an optional callable invoked with a single message string on
    conflict (and on unexpected errors).  Defaults to an ERROR log.
    """

    def _emit_alert(message: str) -> None:
        if alert is not None:
            try:
                alert(message)
            except Exception:
                logger.exception("fork-tracking: alert callback raised")
        logger.error("fork-tracking ALERT: %s", message)

    # Guard: never move main while a worker is running or the tree is unsafe.
    blocked = branch_move_blocked(git_cmd, cwd, exclude_task_id=exclude_task_id)
    if blocked is not None:
        return MergeResult(
            status="blocked",
            detail=blocked,
            head_sha=_head_sha(git_cmd, cwd),
        )

    pre_sha = _head_sha(git_cmd, cwd)

    # 1. Fetch both remotes.
    for remote in (cfg.fork_remote, cfg.upstream_remote):
        fetch = _run_git(git_cmd, cwd, "fetch", remote, "--quiet")
        if fetch.returncode != 0:
            return MergeResult(
                status="error",
                detail=f"git fetch {remote} failed: {fetch.stderr.strip()}",
                head_sha=pre_sha,
            )

    # 2. Fast-forward local branch to fork/branch if the fork advanced.
    fork_ahead = _count_commits(git_cmd, cwd, f"HEAD..{cfg.fork_ref}")
    if fork_ahead > 0:
        ff = _run_git(git_cmd, cwd, "merge", "--ff-only", cfg.fork_ref)
        if ff.returncode != 0:
            # Local has commits the fork doesn't and can't ff — that's a
            # genuine divergence between local and the fork, a human call.
            _emit_alert(
                f"local {cfg.branch} cannot fast-forward to {cfg.fork_ref} "
                f"(diverged); manual reconciliation needed. stderr: "
                f"{ff.stderr.strip()}"
            )
            return MergeResult(
                status="conflict",
                detail=f"cannot ff local to {cfg.fork_ref}",
                head_sha=_head_sha(git_cmd, cwd),
            )

    # 3. Merge upstream into the fork.
    upstream_ahead = _count_commits(git_cmd, cwd, f"HEAD..{cfg.upstream_ref}")
    if upstream_ahead < 0:
        return MergeResult(
            status="error",
            detail=f"could not count commits behind {cfg.upstream_ref}",
            head_sha=_head_sha(git_cmd, cwd),
        )
    if upstream_ahead == 0:
        head = _head_sha(git_cmd, cwd)
        # Local already contains all of upstream. Push if the fork is behind
        # local (e.g. an earlier ff pulled fork commits we should publish).
        pushed = False
        if push and _count_commits(git_cmd, cwd, f"{cfg.fork_ref}..HEAD") > 0:
            pushed = _push_to_fork(git_cmd, cwd, cfg, head, _emit_alert)
        return MergeResult(
            status="up_to_date",
            detail="local already contains all upstream commits",
            head_sha=head,
            pushed=pushed,
        )

    merge = _run_git(
        git_cmd,
        cwd,
        "merge",
        "--no-ff",
        "--no-edit",
        "-m",
        f"merge: integrate {cfg.upstream_ref} into fork {cfg.branch}",
        cfg.upstream_ref,
    )
    if merge.returncode != 0:
        # Conflict (or other merge failure): abort, leave local on last-good
        # fork state, alert. NEVER reset to upstream.
        abort = _run_git(git_cmd, cwd, "merge", "--abort")
        post_sha = _head_sha(git_cmd, cwd)
        recovered = abort.returncode == 0 and post_sha == pre_sha
        _emit_alert(
            f"upstream merge of {cfg.upstream_ref} into fork {cfg.branch} "
            f"conflicts and needs manual/eng resolution. "
            f"Local left on last-good fork state {pre_sha[:10]} "
            f"(merge --abort {'ok' if recovered else 'FAILED — recover manually'}). "
            f"stderr: {merge.stderr.strip()[:300]}"
        )
        return MergeResult(
            status="conflict",
            detail="upstream merge conflict; aborted, left on fork state",
            head_sha=post_sha,
        )

    merged_sha = _head_sha(git_cmd, cwd)
    pushed = False
    if push:
        pushed = _push_to_fork(git_cmd, cwd, cfg, merged_sha, _emit_alert)

    return MergeResult(
        status="merged",
        detail=f"merged {cfg.upstream_ref} into fork {cfg.branch}",
        head_sha=merged_sha,
        pushed=pushed,
    )


def _push_to_fork(
    git_cmd: List[str],
    cwd: Path,
    cfg: ForkTrackingConfig,
    expected_local_sha: str,
    emit_alert,
) -> bool:
    """Push the merge result to the FORK only, with --force-with-lease.

    Uses ``--force-with-lease=<branch>:<remote-fork-sha>`` so a concurrent
    push to the fork is detected instead of stomped.  Never pushes to the
    upstream remote.
    """
    # Lease against the remote-tracking fork ref we just fetched.
    lease_sha = _rev_parse(git_cmd, cwd, cfg.fork_ref)
    lease_arg = (
        f"--force-with-lease={cfg.branch}:{lease_sha}"
        if lease_sha
        else "--force-with-lease"
    )
    push = _run_git(
        git_cmd,
        cwd,
        "push",
        lease_arg,
        cfg.fork_remote,
        f"HEAD:{cfg.branch}",
    )
    if push.returncode != 0:
        emit_alert(
            f"merge succeeded locally ({expected_local_sha[:10]}) but push to "
            f"{cfg.fork_remote}/{cfg.branch} failed (lease stale or network). "
            f"Local is correct; push manually. stderr: {push.stderr.strip()[:300]}"
        )
        return False
    return True


def _rev_parse(git_cmd: List[str], cwd: Path, ref: str) -> str:
    result = _run_git(git_cmd, cwd, "rev-parse", ref, timeout=10)
    return result.stdout.strip() if result.returncode == 0 else ""
