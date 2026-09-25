"""
OT/ICS Risk Radar - API layer.

REST endpoints for historical queries, a WebSocket for live push.

Live push POLLS storage every POLL_INTERVAL_SECONDS for rows newer
than the last broadcast - simple and correct at this scale (a
handful of machines, a handful of events/sec). The natural upgrade
if this needed to scale to many more clients or lower latency is
Postgres LISTEN/NOTIFY fired from the writers (ingestion consumer,
both detectors) straight into the API, skipping the poll entirely -
not built here because it means touching three already-tested
services for a scale problem this project doesn't actually have yet.

Usage: uvicorn main:app --reload --port 8000
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import Counter, Gauge, CONTENT_TYPE_LATEST, generate_latest

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://radar:radar@localhost:5432/radar")
API_KEY = os.environ.get("API_KEY")  # unset -> auth disabled, for local dev
POLL_INTERVAL_SECONDS = 1.5

REQUESTS_TOTAL = Counter("api_requests_total", "HTTP requests handled", ["path", "status"])
WS_CLIENTS = Gauge("api_websocket_clients_connected", "Currently connected WebSocket clients")

app = FastAPI(title="OT/ICS Risk Radar API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to the real frontend origin before this leaves a laptop
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.middleware("http")
async def count_requests(request: Request, call_next):
    response = await call_next(request)
    REQUESTS_TOTAL.labels(path=request.url.path, status=response.status_code).inc()
    return response


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def require_api_key(x_api_key: str = Header(default=None)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid API key")


@app.get("/health")
def health():
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")


@app.get("/machines", dependencies=[Depends(require_api_key)])
def list_machines():
    query = """
        WITH latest_health AS (
            SELECT DISTINCT ON (machine_id) machine_id, health_score, time
            FROM health_scores ORDER BY machine_id, time DESC
        ),
        open_alert_counts AS (
            SELECT machine_id, count(*) AS open_alerts
            FROM alerts WHERE detected_at > now() - interval '1 hour'
            GROUP BY machine_id
        )
        SELECT
            h.machine_id,
            h.health_score AS latest_health_score,
            h.time AS latest_health_time,
            COALESCE(a.open_alerts, 0) AS recent_alert_count
        FROM latest_health h
        LEFT JOIN open_alert_counts a ON a.machine_id = h.machine_id
        ORDER BY h.machine_id
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query)
        return cur.fetchall()


@app.get("/machines/{machine_id}/telemetry", dependencies=[Depends(require_api_key)])
def machine_telemetry(machine_id: str, limit: int = Query(default=100, le=1000)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT time, vibration_mm_s, temperature_c, current_a FROM telemetry "
            "WHERE machine_id = %s ORDER BY time DESC LIMIT %s",
            (machine_id, limit),
        )
        return cur.fetchall()


@app.get("/machines/{machine_id}/health", dependencies=[Depends(require_api_key)])
def machine_health(machine_id: str, limit: int = Query(default=100, le=1000)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT time, health_score FROM health_scores "
            "WHERE machine_id = %s ORDER BY time DESC LIMIT %s",
            (machine_id, limit),
        )
        return cur.fetchall()


@app.get("/alerts", dependencies=[Depends(require_api_key)])
def list_alerts(
    machine_id: str | None = None,
    alert_type: str | None = Query(default=None, pattern="^(maintenance|security)$"),
    limit: int = Query(default=50, le=500),
):
    clauses, params = [], []
    if machine_id:
        clauses.append("machine_id = %s")
        params.append(machine_id)
    if alert_type:
        clauses.append("alert_type = %s")
        params.append(alert_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT id, machine_id, alert_type, severity, detected_at, description "
            f"FROM alerts {where} ORDER BY detected_at DESC LIMIT %s",
            params,
        )
        return cur.fetchall()


def _default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Not JSON serializable: {obj!r}")


@app.websocket("/ws")
async def live_updates(websocket: WebSocket, api_key: str | None = None):
    # Browsers can't set custom headers on a WebSocket handshake, so
    # the API key (when enabled) comes as a query param here instead
    # of the x-api-key header the REST routes use. Found by grepping
    # for require_api_key after the fact: this route had NO auth
    # check at all before this fix - every REST endpoint correctly
    # enforced API_KEY, but /ws was wide open regardless of it.
    if API_KEY and api_key != API_KEY:
        await websocket.close(code=1008)  # policy violation
        return

    await websocket.accept()
    WS_CLIENTS.inc()
    last_seen = datetime.now(timezone.utc)

    # The client never sends anything, so a naive loop that only
    # touches the socket via send() will not notice a disconnect
    # during a quiet poll cycle with no new rows - confirmed by hand:
    # WS_CLIENTS stayed at 1 indefinitely after a client closed the
    # connection with nothing new to broadcast. This background task's
    # only job is to be sitting on receive() so Starlette can surface
    # the disconnect promptly regardless of what the poll loop is doing.
    async def wait_for_disconnect():
        while True:
            await websocket.receive_text()

    disconnect_task = asyncio.ensure_future(wait_for_disconnect())
    try:
        while True:
            poll_task = asyncio.ensure_future(asyncio.sleep(POLL_INTERVAL_SECONDS))
            done, pending = await asyncio.wait(
                {disconnect_task, poll_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if disconnect_task in done:
                poll_task.cancel()
                break
            now = datetime.now(timezone.utc)
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT time, machine_id, vibration_mm_s, temperature_c, current_a "
                    "FROM telemetry WHERE time > %s ORDER BY time",
                    (last_seen,),
                )
                for row in cur.fetchall():
                    await websocket.send_text(json.dumps({"type": "telemetry", **row}, default=_default))

                cur.execute(
                    "SELECT time, machine_id, health_score FROM health_scores "
                    "WHERE time > %s ORDER BY time",
                    (last_seen,),
                )
                for row in cur.fetchall():
                    await websocket.send_text(json.dumps({"type": "health_score", **row}, default=_default))

                cur.execute(
                    "SELECT id, machine_id, alert_type, severity, detected_at, description "
                    "FROM alerts WHERE detected_at > %s ORDER BY detected_at",
                    (last_seen,),
                )
                for row in cur.fetchall():
                    await websocket.send_text(json.dumps({"type": "alert", **row}, default=_default))
            last_seen = now
    except WebSocketDisconnect:
        pass
    finally:
        disconnect_task.cancel()
        WS_CLIENTS.dec()
