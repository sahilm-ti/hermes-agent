import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent


def _make_agent(session_db: SessionDB, session_id: str = "caller_active") -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            session_id=session_id,
            platform="cli",
        )


def _seed_cross_profile_dbs(tmp_path):
    caller_home = tmp_path / "caller_home"
    target_home = tmp_path / "target_home"
    caller_home.mkdir()
    target_home.mkdir()

    caller_db = SessionDB(caller_home / "state.db")
    target_db = SessionDB(target_home / "state.db")

    caller_db.create_session("caller_only", source="cli")
    caller_db._conn.execute(
        "UPDATE sessions SET title = ? WHERE id = ?",
        ("caller-title-discovery", "caller_only"),
    )
    caller_db.append_message("caller_only", role="user", content="callerneedlexyz")
    caller_db.append_message("caller_only", role="assistant", content="caller browse marker")

    target_db.create_session("target_only", source="cli")
    target_db._conn.execute(
        "UPDATE sessions SET title = ? WHERE id = ?",
        ("target-title-discovery", "target_only"),
    )
    target_db.append_message("target_only", role="user", content="targetneedlexyz")
    target_db.append_message("target_only", role="assistant", content="target browse marker")

    # Deliberately shared id so fallback leakage is easy to detect.
    caller_db.create_session("shared_read", source="cli")
    caller_db.append_message("shared_read", role="user", content="caller read transcript")

    target_db.create_session("shared_read", source="cli")
    target_db.append_message("shared_read", role="user", content="target read transcript")

    # Exists only in caller; explicit profile reads for this id must error.
    caller_db.create_session("missing_in_target", source="cli")
    caller_db.append_message("missing_in_target", role="user", content="caller-only fallback bait")

    caller_db._conn.commit()
    target_db._conn.commit()

    return caller_db, target_db, caller_home, target_home


def _patch_profiles(monkeypatch, caller_home, target_home):
    from collections import namedtuple
    from hermes_cli import profiles as profiles_mod

    Info = namedtuple("Info", "name path")
    monkeypatch.setattr(Path, "home", lambda: caller_home.parent)
    monkeypatch.setattr(profiles_mod, "normalize_profile_name", lambda n: n)
    monkeypatch.setattr(profiles_mod, "validate_profile_name", lambda n: None)
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda n: n in {"default", "target"})
    monkeypatch.setattr(
        profiles_mod,
        "get_profile_dir",
        lambda n: target_home if n == "target" else caller_home,
    )
    monkeypatch.setattr(profiles_mod, "list_profiles", lambda: [Info("target", target_home)])


def _invoke_via_sequential(agent: AIAgent, args: dict) -> dict:
    tool_call = SimpleNamespace(
        id="tc-1",
        function=SimpleNamespace(
            name="session_search",
            arguments=json.dumps(args),
        ),
    )
    assistant_message = SimpleNamespace(tool_calls=[tool_call])
    messages = []
    agent._execute_tool_calls_sequential(assistant_message, messages, "task-id")
    return json.loads(messages[-1]["content"])


def _invoke_via_runtime(agent: AIAgent, args: dict) -> dict:
    return json.loads(agent._invoke_tool("session_search", args, "task-id"))


@pytest.mark.parametrize(
    ("invoke_fn", "shape_args", "assertion"),
    [
        (
            _invoke_via_runtime,
            {"profile": "target"},
            lambda payload: (
                payload["mode"] == "browse"
                and {row["session_id"] for row in payload["results"]} == {"target_only", "shared_read"}
            ),
        ),
        (
            _invoke_via_runtime,
            {"profile": "target", "query": "target-title-discovery"},
            lambda payload: (
                payload["mode"] == "discover"
                and payload["count"] >= 1
                and all(hit["session_id"] != "caller_only" for hit in payload["results"])
                and any(hit["session_id"] == "target_only" for hit in payload["results"])
            ),
        ),
        (
            _invoke_via_runtime,
            {"profile": "target", "session_id": "shared_read"},
            lambda payload: (
                payload["mode"] == "read"
                and payload["success"] is True
                and payload["session_id"] == "shared_read"
                and any("target read transcript" in (m.get("content") or "") for m in payload["messages"])
                and all("caller read transcript" not in (m.get("content") or "") for m in payload["messages"])
            ),
        ),
        (
            _invoke_via_sequential,
            {"profile": "target"},
            lambda payload: (
                payload["mode"] == "browse"
                and {row["session_id"] for row in payload["results"]} == {"target_only", "shared_read"}
            ),
        ),
        (
            _invoke_via_sequential,
            {"profile": "target", "query": "target-title-discovery"},
            lambda payload: (
                payload["mode"] == "discover"
                and payload["count"] >= 1
                and all(hit["session_id"] != "caller_only" for hit in payload["results"])
                and any(hit["session_id"] == "target_only" for hit in payload["results"])
            ),
        ),
        (
            _invoke_via_sequential,
            {"profile": "target", "session_id": "shared_read"},
            lambda payload: (
                payload["mode"] == "read"
                and payload["success"] is True
                and payload["session_id"] == "shared_read"
                and any("target read transcript" in (m.get("content") or "") for m in payload["messages"])
                and all("caller read transcript" not in (m.get("content") or "") for m in payload["messages"])
            ),
        ),
    ],
)
def test_session_search_profile_scope_is_honored_for_agent_paths(
    tmp_path,
    monkeypatch,
    invoke_fn,
    shape_args,
    assertion,
):
    caller_db, _target_db, caller_home, target_home = _seed_cross_profile_dbs(tmp_path)
    _patch_profiles(monkeypatch, caller_home, target_home)
    agent = _make_agent(caller_db)

    payload = invoke_fn(agent, shape_args)

    assert assertion(payload), payload


@pytest.mark.parametrize("invoke_fn", [_invoke_via_runtime, _invoke_via_sequential])
def test_session_search_explicit_profile_read_does_not_fallback_to_other_profiles(
    tmp_path,
    monkeypatch,
    invoke_fn,
):
    caller_db, _target_db, caller_home, target_home = _seed_cross_profile_dbs(tmp_path)
    _patch_profiles(monkeypatch, caller_home, target_home)
    agent = _make_agent(caller_db)

    payload = invoke_fn(
        agent,
        {"profile": "target", "session_id": "missing_in_target"},
    )

    assert payload["success"] is False, payload
    assert "session_id not found: missing_in_target" in payload.get("error", "")
