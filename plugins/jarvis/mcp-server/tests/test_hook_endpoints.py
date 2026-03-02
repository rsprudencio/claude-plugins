"""Unit tests for tools.hook_endpoints."""

from unittest.mock import MagicMock

import tools.hook_endpoints as hook_endpoints


def test_get_prompt_context_disabled(monkeypatch):
    """Disabled config returns empty context and skips search."""
    monkeypatch.setattr(
        hook_endpoints,
        "get_per_prompt_config",
        lambda: {"enabled": False, "budget": 9000, "debug": True},
    )
    monkeypatch.setattr(
        hook_endpoints,
        "get_todoist_prompt_alerts_config",
        lambda: {"enabled": True, "max_per_category": 2},
    )
    semantic = MagicMock()
    monkeypatch.setattr(hook_endpoints, "semantic_context", semantic)

    result = hook_endpoints.get_prompt_context("plan next sprint")

    assert result["success"] is True
    assert result["enabled"] is False
    assert result["matches"] == []
    assert result["budget_used"]["total"] == 9000
    assert result["todoist_prompt_alerts"]["enabled"] is True
    semantic.assert_not_called()


def test_get_prompt_context_with_matches(monkeypatch):
    """Prompt context delegates to semantic_context and returns matches."""
    monkeypatch.setattr(
        hook_endpoints,
        "get_per_prompt_config",
        lambda: {"enabled": True, "threshold": 0.6, "budget": 1200, "debug": False},
    )
    monkeypatch.setattr(
        hook_endpoints,
        "get_todoist_prompt_alerts_config",
        lambda: {"enabled": False, "max_per_category": 3},
    )

    semantic = MagicMock(
        return_value={
            "matches": [{"source": "notes/a.md", "relevance": 0.82}],
            "query_ms": 7.2,
            "total_searched": 4,
            "budget_used": {"core": 200, "vault": 300, "total": 1200},
        }
    )
    monkeypatch.setattr(hook_endpoints, "semantic_context", semantic)

    result = hook_endpoints.get_prompt_context("what are my goals")

    semantic.assert_called_once_with(
        query="what are my goals",
        threshold=0.6,
        budget=1200,
        skip_retrieval_increment=False,
    )
    assert result["success"] is True
    assert len(result["matches"]) == 1
    assert result["query_ms"] == 7.2
    assert result["total_searched"] == 4


def test_ingest_auto_extract_observation_dedup_threshold(monkeypatch):
    """Observation dedup threshold controls whether write occurs."""
    monkeypatch.setattr(
        hook_endpoints,
        "query_vault",
        lambda **kwargs: {
            "success": True,
            "results": [{"relevance": 0.97, "preview": "existing"}],
        },
    )
    monkeypatch.setattr(
        hook_endpoints,
        "content_list",
        lambda **kwargs: {"success": True, "documents": []},
    )

    write = MagicMock(return_value={"success": True, "id": "obs::new"})
    monkeypatch.setattr(hook_endpoints, "content_write", write)

    payload = {
        "observations": [{"content": "Same idea", "importance_score": 0.6, "tags": []}],
        "worklog": None,
        "context": {"session_id": "s1", "transcript_line": 10},
        "dedup": {"observation_threshold": 0.95, "worklog_threshold": 0.7},
    }
    dedup_result = hook_endpoints.ingest_auto_extract(payload)
    assert dedup_result["observations"][0]["status"] == "duplicate"
    write.assert_not_called()

    payload["dedup"]["observation_threshold"] = 0.99
    store_result = hook_endpoints.ingest_auto_extract(payload)
    assert store_result["observations"][0]["status"] == "stored"
    write.assert_called_once()


def test_ingest_auto_extract_worklog_dedup_by_session(monkeypatch):
    """Worklog dedup uses session-filtered documents and Jaccard threshold."""
    monkeypatch.setattr(
        hook_endpoints,
        "query_vault",
        lambda **kwargs: {"success": True, "results": []},
    )
    content_list = MagicMock(
        return_value={
            "success": True,
            "documents": [{"content": "Adding docker support to jarvis plugin"}],
        }
    )
    monkeypatch.setattr(hook_endpoints, "content_list", content_list)

    write = MagicMock(return_value={"success": True, "id": "worklog::123"})
    monkeypatch.setattr(hook_endpoints, "content_write", write)

    payload = {
        "observations": [],
        "worklog": {
            "task_summary": "Adding docker support to jarvis plugin",
            "workstream": "Jarvis Plugin",
            "activity_type": "coding",
            "tags": [],
        },
        "context": {"session_id": "session-42", "transcript_line": 30},
        "dedup": {"observation_threshold": 0.95, "worklog_threshold": 0.6},
    }
    result = hook_endpoints.ingest_auto_extract(payload)

    assert result["worklog"]["status"] == "duplicate"
    content_list.assert_called_once_with(
        content_type="worklog",
        session_id="session-42",
        sort_by="created_at_desc",
    )
    write.assert_not_called()


def test_ingest_event_id_passthrough(monkeypatch):
    """ingest_event_id is forwarded into content_write extra_metadata."""
    monkeypatch.setattr(
        hook_endpoints,
        "query_vault",
        lambda **kwargs: {"success": True, "results": []},
    )
    monkeypatch.setattr(
        hook_endpoints,
        "content_list",
        lambda **kwargs: {"success": True, "documents": []},
    )

    calls = []

    def _fake_content_write(**kwargs):
        calls.append(kwargs)
        content_type = kwargs["content_type"]
        doc_id = "obs::1" if content_type == "observation" else "worklog::1"
        return {"success": True, "id": doc_id}

    monkeypatch.setattr(hook_endpoints, "content_write", _fake_content_write)

    payload = {
        "observations": [
            {
                "content": "User prefers concise answers",
                "importance_score": 0.6,
                "tags": ["style"],
                "scope": "global",
                "ingest_event_id": "obs:event:123",
            }
        ],
        "worklog": {
            "task_summary": "Refactor hook transport",
            "workstream": "Jarvis Plugin",
            "activity_type": "coding",
            "tags": ["hooks"],
            "ingest_event_id": "worklog:event:456",
        },
        "context": {
            "project_path": "/tmp/project",
            "git_branch": "main",
            "relevant_files": ["a.py"],
            "file_mtimes": {"a.py": 123.4},
            "session_id": "s-1",
            "transcript_line": 99,
        },
        "dedup": {"observation_threshold": 0.95, "worklog_threshold": 0.7},
    }
    result = hook_endpoints.ingest_auto_extract(payload)
    assert result["success"] is True
    assert len(calls) == 2

    obs_call = next(call for call in calls if call["content_type"] == "observation")
    wl_call = next(call for call in calls if call["content_type"] == "worklog")
    assert obs_call["extra_metadata"]["ingest_event_id"] == "obs:event:123"
    assert wl_call["extra_metadata"]["ingest_event_id"] == "worklog:event:456"
