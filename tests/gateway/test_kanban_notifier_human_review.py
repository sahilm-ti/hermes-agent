"""Tests for the gateway kanban notifier's human_review event path.

Pins the new event-kind rendering added alongside the ``human_review`` status:

* ``human_review_requested`` — fires the ⏳ "ready for your review" message.
* ``approved`` — fires the ✅ "approved" message.
* ``rejected`` — fires the ↩ "rejected — back to ready" message.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _setup_subscribed_task():
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="human-review-flow", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        task = kb.claim_task(conn, tid)
        assert task is not None
        return tid
    finally:
        conn.close()


def test_notifier_renders_human_review_requested(tmp_path, monkeypatch):
    db_path = tmp_path / "human-review.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _setup_subscribed_task()
    conn = kb.connect()
    try:
        ok = kb.move_to_human_review(
            conn, tid, reason="PR https://example.com/pr/42 merged; please approve",
        )
        assert ok
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert "⏳" in text
    assert tid in text
    assert "PR https://example.com/pr/42" in text


def test_notifier_renders_approved(tmp_path, monkeypatch):
    db_path = tmp_path / "approved.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _setup_subscribed_task()
    conn = kb.connect()
    try:
        assert kb.move_to_human_review(conn, tid, reason="ready")
        assert kb.approve_task(conn, tid, reason="LGTM")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Two messages expected: human_review_requested then approved.
    kinds_seen = " | ".join(m["text"] for m in adapter.sent)
    assert "✅" in kinds_seen
    assert "approved" in kinds_seen.lower()
    assert tid in kinds_seen


def test_notifier_renders_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "rejected.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _setup_subscribed_task()
    conn = kb.connect()
    try:
        assert kb.move_to_human_review(conn, tid, reason="ready")
        assert kb.reject_task(conn, tid, reason="needs tests for edge case")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    text_all = " | ".join(m["text"] for m in adapter.sent)
    assert "↩" in text_all
    assert "rejected" in text_all.lower()
    assert "needs tests" in text_all
