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


def test_simulator_passes_the_augmentation_era_filter(monkeypatch):
    """Phase-2 calibration must be able to restrict the pool to one augmentation
    era: mechanical-era and summary-era BGE logits are not comparable (the same
    chunk scored -8.16 and +0.03), so a threshold swept across both is correct
    for neither."""
    seen = {}
    monkeypatch.setattr(
        telemetry, "simulate_policy",
        lambda payload: seen.update(payload) or {"policy": payload["policy"]},
    )
    client = TestClient(app_module.app)

    client.post(
        "/api/retrieval/simulate",
        json={"policy": "bge-only", "contextual_augmentation": "summary"},
    )
    assert seen["contextual_augmentation"] == "summary"

    # Absent means "all eras pooled" — and the caller can see that in the reply.
    seen.clear()
    client.post("/api/retrieval/simulate", json={"policy": "bge-only"})
    assert seen["contextual_augmentation"] == ""


def test_simulator_rejects_an_invalid_era(monkeypatch):
    def boom(payload):
        raise ValueError("invalid contextual_augmentation filter")

    monkeypatch.setattr(telemetry, "simulate_policy", boom)
    response = TestClient(app_module.app).post(
        "/api/retrieval/simulate",
        json={"policy": "bge-only", "contextual_augmentation": "bogus"},
    )
    assert response.status_code == 400


def test_simulator_ui_exposes_the_era_selector():
    response = TestClient(app_module.app).get("/")
    assert 'id="sim-era"' in response.text
    assert 'value="summary"' in response.text
    assert "augmentation_eras_mixed" in response.text
