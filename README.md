# OT/ICS risk radar

A real-time data pipeline that watches simulated industrial telemetry
for two things at once: equipment drifting toward failure
(predictive maintenance) and command patterns that look like a
security anomaly (OT/ICS security) — one dashboard, two risk types.

Full system design, data model, and the real build story (including
two anomaly-detection designs that failed testing before landing on
the one that works): see [`docs/DESIGN.md`](docs/DESIGN.md).

## Status

**Milestones 1-6 done, M7 (production hardening) mostly done.**
Simulator, ingestion, both detectors, API, and dashboard are all
built and tested for real — plus CI and Prometheus metrics across
every service. Kafka is documented as a concrete plan in
`docs/DESIGN.md` but deliberately not built (no real broker was
reachable to test against). See `docs/DESIGN.md` section 7 for the
roadmap and section 8 for what each milestone actually proves.

## Metrics

Every service exposes Prometheus-format `/metrics`:

| Service | Port |
|---|---|
| API | 8000 (`/metrics`) |
| Ingestion consumer | 9101 |
| Maintenance detector | 9102 |
| Security detector | 9103 |

## Continuous integration

`.github/workflows/ci.yml` — a `unit-tests` job (no infra needed,
runs the three pure-logic test suites) and a `live-smoke-test` job
(real Postgres + Mosquitto + all three services + the simulator,
asserting real rows land). Both jobs' exact command sequences were
validated by hand in a throwaway environment before being committed.

## Quick start

Six terminals (or run each in the background) — a broker, a
database, and four Python services, plus the static dashboard:

```bash
# 1. Start the broker + database
docker compose up -d
cp .env.example .env

# 2. Simulator
cd simulator && pip install -r requirements.txt
python simulator.py

# 3. Ingestion (separate terminal)
cd ingestion && pip install -r requirements.txt
python consumer.py

# 4. Predictive maintenance detector (separate terminal)
cd detection && pip install -r requirements.txt
python maintenance_detector.py

# 5. OT security detector (separate terminal)
cd detection
python security_detector.py

# 6. API (separate terminal)
cd api && pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# 7. Dashboard — just open frontend/index.html, or serve it:
cd frontend && python -m http.server 8080
# then visit http://localhost:8080
# If the API is running with API_KEY set, the dashboard needs it too
# (for both REST calls and the WebSocket, which enforces the key via
# ?api_key= since browsers can't set custom headers on a WebSocket
# handshake). It's read once at page load, so set it before opening
# the page - add this line in index.html's <head>, before the
# Babel <script type="text/babel"> block:
#   <script>window.RADAR_API_KEY = "your-key-here";</script>
```

Check data is actually landing:

```bash
curl localhost:8000/machines
curl localhost:8000/alerts
```

## Run the tests

```bash
cd simulator  && python test_machines.py
cd detection  && python test_baseline.py         # CUSUM detector, multi-seed validation
cd detection  && python test_security_rules.py   # includes a 4,000-command integration check
cd api        && DATABASE_URL=postgresql://radar:radar@localhost:5432/radar python test_main.py
```

## Repo layout

```
ot-ics-risk-radar/
├── docker-compose.yml     # broker + database
├── .env.example
├── mosquitto/              # broker config
├── db/
│   └── schema.sql           # 5 tables, applied automatically on first db start
├── simulator/               # milestone 1
│   ├── machines.py          # simulation logic (pure, unit-tested)
│   ├── simulator.py         # MQTT publishing loop
│   ├── test_machines.py
│   └── requirements.txt
├── ingestion/                # milestone 2
│   ├── consumer.py          # MQTT -> Postgres, idempotent, dead-letters bad input
│   └── requirements.txt
├── detection/                 # milestones 3 + 4
│   ├── baseline.py           # maintenance: CUSUM detector (see its docstring for the design story)
│   ├── test_baseline.py
│   ├── maintenance_detector.py
│   ├── security_rules.py     # security: declared config, not learned
│   ├── test_security_rules.py
│   ├── security_detector.py
│   └── requirements.txt
├── api/                        # milestone 5
│   ├── main.py                # FastAPI: REST + polling WebSocket + /metrics
│   ├── test_main.py           # 12 tests against the real DB, incl. both auth paths
│   └── requirements.txt
├── frontend/                    # milestone 6
│   └── index.html             # single-file, React via CDN, no build step
├── .github/workflows/
│   └── ci.yml                 # milestone 7 — unit-tests + live-smoke-test jobs
└── docs/
    └── DESIGN.md               # full system design + build history + Kafka plan
```

Tracing and the Kafka swap-in are the two remaining M7 items — see
`docs/DESIGN.md` section 6 for both, including why Kafka is a written
plan rather than code.
