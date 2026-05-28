"""Tests for reply context propagation through /queue and /steer event construction.

When a user replies to a message and uses /queue or /steer, the queued
MessageEvent must carry reply_to_message_id and reply_to_text so that
_prepare_inbound_message_text can inject the [Replying to: "..."] prefix
when the turn eventually runs.

Regression for: t_1cc95bc9 — /queue drops reply_to_message_id + reply_to_text
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    runner._queued_events = {}
    return runner


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_name="DM",
        chat_type="private",
        user_name="Alice",
    )


def _make_adapter():
    adapter = MagicMock()
    adapter._pending_messages = {}
    return adapter


# ---------------------------------------------------------------------------
# /queue reply context propagation
# ---------------------------------------------------------------------------

class TestQueueReplyContextPropagation:
    """/queue must copy reply_to_message_id and reply_to_text onto the queued event."""

    def test_queue_preserves_reply_to_message_id(self):
        runner = _make_runner()
        adapter = _make_adapter()
        session_key = "telegram:123"

        inbound = MessageEvent(
            text="/queue do this next",
            message_type=MessageType.TEXT,
            source=_source(),
            message_id="m1",
            reply_to_message_id="999",
            reply_to_text="The task has been approved.",
        )

        queued_event = MessageEvent(
            text="do this next",
            message_type=MessageType.TEXT,
            source=inbound.source,
            message_id=inbound.message_id,
            channel_prompt=inbound.channel_prompt,
            reply_to_message_id=getattr(inbound, "reply_to_message_id", None),
            reply_to_text=getattr(inbound, "reply_to_text", None),
        )
        runner._enqueue_fifo(session_key, queued_event, adapter)

        stored: MessageEvent = adapter._pending_messages[session_key]
        assert stored.reply_to_message_id == "999"
        assert stored.reply_to_text == "The task has been approved."

    def test_queue_without_reply_context_is_none(self):
        runner = _make_runner()
        adapter = _make_adapter()
        session_key = "telegram:123"

        inbound = MessageEvent(
            text="/queue plain prompt",
            message_type=MessageType.TEXT,
            source=_source(),
            message_id="m2",
        )

        queued_event = MessageEvent(
            text="plain prompt",
            message_type=MessageType.TEXT,
            source=inbound.source,
            message_id=inbound.message_id,
            channel_prompt=inbound.channel_prompt,
            reply_to_message_id=getattr(inbound, "reply_to_message_id", None),
            reply_to_text=getattr(inbound, "reply_to_text", None),
        )
        runner._enqueue_fifo(session_key, queued_event, adapter)

        stored: MessageEvent = adapter._pending_messages[session_key]
        assert stored.reply_to_message_id is None
        assert stored.reply_to_text is None

    def test_enqueue_fifo_preserves_reply_context_across_overflow(self):
        """reply context must survive on each item individually in a multi-/queue chain."""
        runner = _make_runner()
        adapter = _make_adapter()
        session_key = "telegram:123"

        # First item has reply context; second does not.
        ev1 = MessageEvent(
            text="first",
            message_type=MessageType.TEXT,
            source=_source(),
            message_id="q1",
            reply_to_message_id="42",
            reply_to_text="Approve the PR",
        )
        ev2 = MessageEvent(
            text="second",
            message_type=MessageType.TEXT,
            source=_source(),
            message_id="q2",
        )

        runner._enqueue_fifo(session_key, ev1, adapter)
        runner._enqueue_fifo(session_key, ev2, adapter)

        slot_event: MessageEvent = adapter._pending_messages[session_key]
        assert slot_event.reply_to_message_id == "42"
        assert slot_event.reply_to_text == "Approve the PR"

        overflow_event: MessageEvent = runner._queued_events[session_key][0]
        assert overflow_event.reply_to_message_id is None
        assert overflow_event.reply_to_text is None


# ---------------------------------------------------------------------------
# Integration: reply prefix appears when queued event is processed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queued_event_with_reply_context_gets_prefix_injected():
    """End-to-end: a queued event carrying reply_to_text produces the
    [Replying to: "..."] prefix when _prepare_inbound_message_text runs."""
    runner = _make_runner()
    source = _source()

    queued_event = MessageEvent(
        text="this is approved",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m3",
        reply_to_message_id="999",
        reply_to_text="Task t_8698e650 is ready for your review.",
    )

    result = await runner._prepare_inbound_message_text(
        event=queued_event,
        source=source,
        history=[],
    )

    assert result is not None
    assert '[Replying to: "Task t_8698e650 is ready for your review."]' in result
    assert result.endswith("this is approved")


@pytest.mark.asyncio
async def test_queued_event_without_reply_context_has_no_prefix():
    """A queued event with no reply context must NOT get a spurious prefix."""
    runner = _make_runner()
    source = _source()

    queued_event = MessageEvent(
        text="plain queued message",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m4",
    )

    result = await runner._prepare_inbound_message_text(
        event=queued_event,
        source=source,
        history=[],
    )

    assert result == "plain queued message"
