"""Tests for the API layer. Run: python test_main.py

Uses FastAPI's TestClient against the REAL database (DATABASE_URL,
same as every other service) rather than mocking the DB - consistent
with how the rest of this repo tests things: real Postgres, real
MQTT, real integration, not mocks standing in for the actual system.
Requires the same local Postgres the other test/run instructions do.
"""

import main
from fastapi.testclient import TestClient

client = TestClient(main.app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_machines_shape():
    r = client.get("/machines")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    if body:  # DB may be empty on a truly fresh run
        row = body[0]
        assert {"machine_id", "latest_health_score", "latest_health_time", "recent_alert_count"} <= row.keys()


def test_alerts_default_limit_and_shape():
    r = client.get("/alerts")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) <= 50  # default limit
    if body:
        assert body[0]["alert_type"] in ("maintenance", "security")


def test_alerts_filter_by_valid_type():
    r = client.get("/alerts?alert_type=security")
    assert r.status_code == 200
    assert all(row["alert_type"] == "security" for row in r.json())


def test_alerts_reject_invalid_type():
    r = client.get("/alerts?alert_type=not_a_real_type")
    assert r.status_code == 422


def test_alerts_limit_is_capped():
    r = client.get("/alerts?limit=99999")
    assert r.status_code == 422  # over the le=500 cap


def test_machine_telemetry_respects_limit():
    r = client.get("/machines/press-01/telemetry?limit=3")
    assert r.status_code == 200
    assert len(r.json()) <= 3


def test_machine_telemetry_unknown_machine_returns_empty_not_error():
    r = client.get("/machines/does-not-exist-999/telemetry")
    assert r.status_code == 200
    assert r.json() == []


def test_metrics_endpoint_exposes_request_counter():
    client.get("/health")  # generate at least one countable request
    r = client.get("/metrics")
    assert r.status_code == 200
    assert b"api_requests_total" in r.content
    assert b"api_websocket_clients_connected" in r.content


def test_auth_disabled_by_default():
    # main.API_KEY is None unless API_KEY was set in the environment
    # before the module was imported - this test only means what it
    # says when run without API_KEY set, same as the documented
    # local-dev default.
    if main.API_KEY:
        return  # skip: this process happens to have auth enabled
    r = client.get("/machines")
    assert r.status_code == 200


def test_auth_enforced_when_key_set():
    original = main.API_KEY
    main.API_KEY = "test-suite-secret"
    try:
        assert client.get("/machines").status_code == 401
        assert client.get("/machines", headers={"x-api-key": "wrong"}).status_code == 401
        assert client.get("/machines", headers={"x-api-key": "test-suite-secret"}).status_code == 200
        # /health must stay open regardless - load balancers can't send a key
        assert client.get("/health").status_code == 200
    finally:
        main.API_KEY = original  # restore, so later tests in this file see the real default


def test_websocket_requires_key_when_set():
    # Browsers can't set custom headers on a WS handshake, so this is
    # a query param (?api_key=...), not the x-api-key header the REST
    # routes use - see the comment on the route itself for why this
    # check exists at all (it originally didn't, on any route).
    from starlette.websockets import WebSocketDisconnect

    original = main.API_KEY
    main.API_KEY = "test-suite-secret"
    try:
        try:
            with client.websocket_connect("/ws"):
                raise AssertionError("connected with no key - should have been rejected")
        except WebSocketDisconnect as e:
            assert e.code == 1008, f"expected close code 1008, got {e.code}"

        with client.websocket_connect("/ws?api_key=test-suite-secret") as ws:
            pass  # connecting without raising is the assertion
    finally:
        main.API_KEY = original


if __name__ == "__main__":
    tests = [
        test_health_ok,
        test_machines_shape,
        test_alerts_default_limit_and_shape,
        test_alerts_filter_by_valid_type,
        test_alerts_reject_invalid_type,
        test_alerts_limit_is_capped,
        test_machine_telemetry_respects_limit,
        test_machine_telemetry_unknown_machine_returns_empty_not_error,
        test_metrics_endpoint_exposes_request_counter,
        test_auth_disabled_by_default,
        test_auth_enforced_when_key_set,
        test_websocket_requires_key_when_set,
    ]
    for t in tests:
        t()
        print(f"{t.__name__} passed")
    print("All tests passed.")
