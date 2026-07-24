from fastapi.testclient import TestClient

import app as app_module
import tools.retrieval_telemetry as telemetry


def test_retrieval_summary_and_event_routes(monkeypatch):
    monkeypatch.setattr(telemetry, "get_summary", lambda days=7: {"days": days, "requests": 3})
    monkeypatch.setattr(telemetry, "list_events", lambda **kwargs: [{"id": "trace-1"}])
    monkeypatch.setattr(telemetry, "get_event", lambda event_id: {"id": event_id, "candidates": []})
    client = TestClient(app_module.app)

    assert client.get("/api/retrieval/summary?days=3").json()["requests"] == 3
    assert client.get("/api/retrieval/events").json() == [{"id": "trace-1"}]
    assert client.get("/api/retrieval/events/trace-1").json()["id"] == "trace-1"


def test_retrieval_feedback_and_simulator_routes(monkeypatch):
    writes = []
    monkeypatch.setattr(telemetry, "put_event_feedback", lambda event_id, payload, user: writes.append((event_id, payload, user)) or True)
    monkeypatch.setattr(telemetry, "put_candidate_feedback", lambda event_id, key, payload, user: writes.append((event_id, key, payload, user)) or True)
    monkeypatch.setattr(telemetry, "simulate_policy", lambda payload: {"policy": payload["policy"], "selected_count": 2})
    client = TestClient(app_module.app)

    assert client.put("/api/retrieval/events/t1/feedback", json={"verdict": "useful"}).json() == {"ok": True}
    assert client.put("/api/retrieval/events/t1/candidates/c1/feedback", json={"verdict": "relevant"}).json() == {"ok": True}
    result = client.post("/api/retrieval/simulate", json={"policy": "bge-only"}).json()
    assert result == {"policy": "bge-only", "selected_count": 2}
    assert writes[0][2] == "anonymous"


def test_retrieval_tab_is_present():
    response = TestClient(app_module.app).get("/")
    assert response.status_code == 200
    assert 'data-tab="retrieval"' in response.text
    assert "Policy simulator (read-only)" in response.text


def test_retrieval_event_documents_route(monkeypatch):
    captured = {}

    def fake_docs(event_id, preview_chars=240, candidate_key=None):
        captured.update(event_id=event_id, preview_chars=preview_chars, candidate_key=candidate_key)
        return {"abc123": {"found": True, "size": 900, "text": "preview text", "truncated": True}}

    monkeypatch.setattr(telemetry, "get_event_documents", fake_docs)
    client = TestClient(app_module.app)

    body = client.get("/api/retrieval/events/t1/documents?preview_chars=100").json()
    assert body["abc123"]["truncated"] is True
    assert captured == {"event_id": "t1", "preview_chars": 100, "candidate_key": None}

    client.get("/api/retrieval/events/t1/documents?candidate_key=abc123")
    assert captured["candidate_key"] == "abc123"
