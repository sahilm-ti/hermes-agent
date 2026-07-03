"""Shared daemon-thread ThreadPoolExecutor.

Stdlib ``ThreadPoolExecutor`` workers are non-daemon AND are registered in
``concurrent.futures.thread._threads_queues``, whose atexit hook
(``_python_exit``) joins every worker unconditionally — even after
``shutdown(wait=False)``.  A single wedged worker (tool blocked on network
I/O, hung provider daemon, stuck subagent) therefore blocks interpreter
exit forever.  This is the root cause of multi-minute CLI exits on long
sessions: every abandoned concurrent-tool batch leaves workers that the
exit hook insists on joining.

``DaemonThreadPoolExecutor`` spawns daemon workers and skips the
``_threads_queues`` registration, so:

  - ``_python_exit`` never joins them, and
  - the interpreter's non-daemon thread join at shutdown skips them.

Semantics are otherwise identical (initializer/initargs, work queue,
idle-thread reuse).  Use it for any pool whose work is best-effort or
independently interruptible and must never hold the process open:
concurrent tool execution, background memory sync, catalog fan-out,
subagent timeout wrappers.  Do NOT use it for work that must complete
before exit (durable writes) — those belong on foreground threads with
explicit bounded joins.

``_adjust_thread_count`` mirrors CPython's private implementation because
there is no public hook for "spawn a worker without registering it in
``_threads_queues``". CPython changed the internal shape of
``ThreadPoolExecutor`` in 3.14 (bpo-generalized worker "context" object
replacing the old ``(initializer, initargs)`` attribute pair and the old
4-arg ``_worker`` free function — see cpython#113135 / gh-91555). Both
shapes are mirrored below and selected once at import time so this module
keeps working across the 3.11–3.14 range this repo supports
(``pyproject.toml: requires-python = ">=3.11,<3.14"`` plus local dev venvs
that have since moved to 3.14).
"""

from __future__ import annotations

import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor

__all__ = ["DaemonThreadPoolExecutor"]

# CPython 3.14 replaced the (initializer, initargs) attributes + 4-arg
# _worker free function with a WorkerContext object created via
# ThreadPoolExecutor.prepare_context(). Confirmed by inspecting
# concurrent.futures.thread on 3.14.6: ThreadPoolExecutor.__init__ no
# longer sets self._initializer/self._initargs at all (AttributeError on
# access), and _worker's signature is
# (executor_reference, ctx, work_queue) instead of
# (executor_reference, work_queue, initializer, initargs).
_PY314_WORKER_CONTEXT = sys.version_info >= (3, 14)

if _PY314_WORKER_CONTEXT:
    from concurrent.futures.thread import _worker as _worker_py314

    def _spawn_daemon_worker(
        pool: "DaemonThreadPoolExecutor", thread_name: str, weakref_cb
    ) -> threading.Thread:
        ctx = pool._create_worker_context()
        return threading.Thread(
            name=thread_name,
            target=_worker_py314,
            args=(weakref.ref(pool, weakref_cb), ctx, pool._work_queue),
            daemon=True,
        )

else:
    from concurrent.futures.thread import _worker as _worker_legacy

    def _spawn_daemon_worker(
        pool: "DaemonThreadPoolExecutor", thread_name: str, weakref_cb
    ) -> threading.Thread:
        # getattr (not pool._initializer directly): on interpreters where
        # this branch is dead code (>=3.14) these attributes don't exist,
        # and static type checkers running under a newer typeshed target
        # would otherwise flag the direct attribute access even though it
        # never executes at runtime for that version.
        initializer = getattr(pool, "_initializer", None)
        initargs = getattr(pool, "_initargs", ())
        return threading.Thread(
            name=thread_name,
            target=_worker_legacy,
            args=(
                weakref.ref(pool, weakref_cb),
                pool._work_queue,
                initializer,
                initargs,
            ),
            daemon=True,
        )


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor variant whose workers do not block process exit."""

    def _adjust_thread_count(self) -> None:
        # Mirrors CPython's implementation (3.11-3.14, see _spawn_daemon_worker
        # above for the version split) with two changes: daemon=True and no
        # _threads_queues registration.
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
            t = _spawn_daemon_worker(self, thread_name, weakref_cb)
            t.start()
            self._threads.add(t)
