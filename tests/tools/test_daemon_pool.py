"""Tests for tools.daemon_pool.DaemonThreadPoolExecutor.

The daemon pool exists so abandoned workers (interrupted/timed-out tool
batches, wedged memory-provider syncs) can never block interpreter exit:
stdlib ThreadPoolExecutor workers are non-daemon AND registered in
concurrent.futures.thread._threads_queues, whose atexit hook joins every
worker unconditionally — even after shutdown(wait=False).
"""

import subprocess
import sys
import threading
import time

from concurrent.futures.thread import _threads_queues

from tools.daemon_pool import DaemonThreadPoolExecutor


def test_workers_are_daemon_threads():
    pool = DaemonThreadPoolExecutor(max_workers=2)
    try:
        info = pool.submit(
            lambda: (threading.current_thread().daemon, threading.current_thread())
        ).result(timeout=10)
        is_daemon, worker = info
        assert is_daemon is True
        # Not registered with concurrent.futures' atexit join hook.
        assert worker not in _threads_queues
    finally:
        pool.shutdown(wait=True)


def test_results_and_initializer_work_like_stdlib():
    seen = []

    def _init(tag):
        seen.append(tag)

    pool = DaemonThreadPoolExecutor(max_workers=1, initializer=_init, initargs=("t",))
    try:
        assert pool.submit(lambda: 41 + 1).result(timeout=10) == 42
        assert seen == ["t"]
    finally:
        pool.shutdown(wait=True)


def test_idle_worker_reuse():
    pool = DaemonThreadPoolExecutor(max_workers=4)
    try:
        tid1 = pool.submit(threading.get_ident).result(timeout=10)
        time.sleep(0.05)  # let the worker park on the idle semaphore
        tid2 = pool.submit(threading.get_ident).result(timeout=10)
        assert tid1 == tid2
    finally:
        pool.shutdown(wait=True)


def test_many_concurrent_submits_like_tool_executor():
    """Regression test for the 3.14 AttributeError on ``_initializer``.

    ``agent/tool_executor.py`` submits N tool calls to a fresh
    DaemonThreadPoolExecutor whenever the model requests 2+ tool calls in
    the same turn (see ``_run_concurrent_tools``). Before the 3.14
    WorkerContext fix, ``_adjust_thread_count`` read ``self._initializer``/
    ``self._initargs`` directly — attributes that no longer exist on
    ThreadPoolExecutor as of CPython 3.14 (replaced by
    ``prepare_context()``/``WorkerContext``) — and every worker spawn
    raised ``AttributeError: 'DaemonThreadPoolExecutor' object has no
    attribute '_initializer'`` inside the worker thread, silently failing
    every concurrently-submitted tool call. This test reproduces that
    exact shape: more submissions than max_workers, forcing multiple
    ``_adjust_thread_count`` calls, and asserts every future actually
    completes with the right value instead of erroring.
    """
    pool = DaemonThreadPoolExecutor(max_workers=4)
    try:
        futures = [pool.submit(lambda n=i: n * n) for i in range(12)]
        results = [f.result(timeout=10) for f in futures]
        assert results == [n * n for n in range(12)]
    finally:
        pool.shutdown(wait=True)


def test_wedged_worker_does_not_block_interpreter_exit():
    """A worker stuck in a long sleep must not hold the process open.

    With stdlib ThreadPoolExecutor this subprocess hangs until the sleep
    finishes (the atexit hook joins the worker); with the daemon pool it
    exits as soon as the main thread returns.
    """
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from tools.daemon_pool import DaemonThreadPoolExecutor\n"
        "import time\n"
        "pool = DaemonThreadPoolExecutor(max_workers=1)\n"
        "pool.submit(time.sleep, 120)\n"
        "time.sleep(0.3)\n"
        "pool.shutdown(wait=False)\n"
        "print('main-done', flush=True)\n"
    ) % (str(_repo_root()),)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "main-done" in proc.stdout


def _repo_root():
    import pathlib

    return pathlib.Path(__file__).resolve().parents[2]
