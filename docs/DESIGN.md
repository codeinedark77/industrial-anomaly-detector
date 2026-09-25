# OT/ICS Risk Radar — System Design

## 1. Problem statement

Modern factories run OT (operational technology — PLCs, sensors,
SCADA systems) increasingly connected to IT networks. This is what
the industry calls "IT/OT convergence," and it's a genuine, current
tension: it enables the same predictive-maintenance and analytics
that make plants more efficient, but it also exposes industrial
control systems to network-borne threats they were never designed
to withstand.

Most portfolio projects touching industrial data pick one problem —
predictive maintenance *or* security — as if they're unrelated. In
practice, plant operations and OT security teams increasingly watch
the same telemetry stream for different reasons, on the same
platform. This project treats them as one problem: a single
real-time pipeline that scores incoming plant data for both
mechanical risk and security risk.

## 2. Goals & non-goals

**Goals**
- Real-time (sub-few-second latency) ingestion of streaming
  industrial telemetry
- Two independent detection paths, running in parallel off one
  pipeline and one alert schema
- Production-grade *at portfolio scale*: real schemas, real
  retry/idempotency handling, real tests — without pretending to be
  an actual 24/7 SCADA deployment
- Every component individually swappable (detection logic, broker,
  storage engine) without touching the others

**Non-goals**
- Connecting to a real PLC/SCADA system — out of reach, and
  honestly inadvisable outside a sandboxed lab, since real ICS
  protocols assume a trusted network
- State-of-the-art detection accuracy — the modeling here is
  intentionally simple and explainable first, sophisticated second
- Multi-tenant / multi-plant support — this is one simulated plant

## 3. Architecture overview

```
 [Simulator] --MQTT--> [Broker: Mosquitto] --> [Stream processor]
                                                       |
                                 ------------------------------------
                                 |                                  |
                       [Predictive maintenance]         [OT security detector]
                                 |                                  |
                                 ------------------------------------
                                               |
                                   [Storage: TimescaleDB + Postgres]
                                               |
                                   [API: FastAPI + WebSocket]
                                               |
                                   [Dashboard: React]
```

Data flows one direction, start to finish. The only fan-out is the
detection stage, where the same event stream is read by two
independent consumers — they don't know about each other and can be
built, deployed, or replaced separately.

## 4. Component design

### 4.1 Telemetry simulator (`simulator/`) — Milestone 1, done
Generates synthetic vibration, temperature, and current-draw
readings for four virtual machines, with two injected anomaly
types: gradual sensor drift (simulated bearing wear, for the
maintenance path) and occasional out-of-envelope commands (for the
security path). Publishes over MQTT. Logic lives in `machines.py`,
kept independent of the MQTT plumbing in `simulator.py` so it's
unit-testable without a broker running — see `test_machines.py`.

### 4.2 Ingestion — Milestone 2, done
The simulator publishes to `plant/{machine_id}/telemetry` and
`plant/{machine_id}/commands`. Mosquitto is the broker because MQTT
is the protocol real IIoT devices actually speak — that's the
realistic choice, not a shortcut.

For the stream processor there was a genuine build-vs-buy call:
- **Lean path** — a Python consumer subscribed directly to the MQTT
  topics, doing detection inline. Fastest to build, and fully
  sufficient at this data volume. **Built.**
- **Data-engineer-signal path** — bridge MQTT into Kafka and have
  the stream processor read from there instead. Heavier lift, but
  it's what actually demonstrates partitioning, consumer groups,
  and replay. Deferred — the lean path proved out the schema and
  detection interface first, Kafka slots in behind it later without
  changing anything downstream.

`ingestion/consumer.py` (see repo) does three things beyond the
naive version:
- **Idempotent writes** — every insert is an upsert on
  `(machine_id, time)`, so an MQTT redelivery (QoS 1 guarantees
  at-least-once, which means *possibly more than once*) can't
  double-count a reading. Verified by hand: republishing an exact
  duplicate message left the row count unchanged.
- **Dead-lettering, not dropping** — malformed JSON or a payload
  missing required fields gets written to a `dead_letters` table
  with the topic, reason, and raw content, instead of vanishing
  into a log line no one reads.
- **A callback that can't take the process down** — the first
  version let a malformed-payload exception propagate out of
  paho-mqtt's `on_message`, which kills `loop_forever()` and the
  whole consumer with it. Caught this by actually publishing a
  non-JSON message at test time, not by reasoning about it — it's
  now wrapped so message handling failures are logged and
  dead-lettered instead of fatal. Worth keeping as a specific
  example of why "add error handling" needs to mean tested error
  handling, not just a try/except that looks right on paper.

### 4.3 Predictive maintenance detector — Milestone 3, done
Full design history is in `detection/baseline.py`'s module
docstring, kept there deliberately because the dead ends are the
real content. Short version: a first design (adapting EWMA baseline,
raw z-score, "N consecutive readings over threshold") was tested
against the simulator's actual drift pattern and **never fired** -
instrumenting it and printing z-scores tick by tick showed the
baseline was adapting fast enough to chase the ramp instead of
diverging from it. A second attempt (stop updating the baseline on
anomalous points) still let ordinary per-sample noise reset the
consecutive-count streak often enough mid-ramp to prevent it from
ever reaching threshold.

The design that actually works: freeze the baseline after warmup
(deliberately - a baseline meant to detect degradation can't also be
built to adapt to it), and use a CUSUM (cumulative sum) instead of a
consecutive-count streak. CUSUM is the standard statistical-process-
control technique for exactly this problem - a small *sustained*
shift, not a single outlier - and it's why it's robust to the same
noise that broke attempt two: it accumulates small deviations over
time rather than requiring any individual reading to be extreme.

Validated across 5 random seeds: zero false positives over 400
stable ticks each, consistent detection at tick 11-14 of the
simulator's 200-tick drift ramp (`detection/test_baseline.py`). Also
confirmed live through the real pipeline, not just in isolated unit
tests: a fast-forwarded warmup-plus-drift sequence published over
real MQTT into the real consumer correctly produced `maintenance`
alerts for both `vibration_mm_s` and `temperature_c`, and
`health_score` dropped from 90.8 to 0.0 across the episode.

### 4.4 OT security detector — Milestone 4, done
Rule-based, deliberately not learned: `detection/security_rules.py`
declares a safe value range and a known-source-IP allowlist per
machine - the kind of thing a real deployment would pull from an
asset inventory and engineering spec, not infer from traffic. That
matters here specifically: learning "normal" from observed traffic
would let an attacker's IP get quietly allow-listed just by issuing
enough commands before the detector started watching.

Validated with a real integration check, not just synthetic unit
cases: `test_security_rules.py` runs the actual simulator's command
generator - which carries its own internal "is this anomalous" flag
that the rules never see - for 20,000 ticks (4,000+ real commands
generated) and confirms the rules agreed with that ground truth on
every single one. Two simultaneous rule violations (bad value *and*
unknown source) are scored `critical`; one alone is `warning` - a
legitimate engineer fat-fingering a setpoint is still coming from a
known IP.

### 4.5 Storage — done
TimescaleDB (a Postgres extension) chosen over a dedicated
time-series DB specifically because it's still real SQL - matters
both for the interview story and for writing the ad hoc analytical
queries a plant engineer would actually want. See section 5.

### 4.6 API layer — Milestone 5, done
FastAPI. `GET /health` (always open, for load balancers), `GET
/machines` (latest health + recent alert count per machine), `GET
/machines/{id}/telemetry`, `GET /machines/{id}/health`, `GET
/alerts` (filterable by machine and type). All routes except
`/health` sit behind an optional API-key dependency - disabled by
default for local dev, enforced the moment `API_KEY` is set in the
environment. Verified live: 401 with no key or the wrong key, 200
with the right one, `/health` open regardless.

Live push is `GET /ws`, a WebSocket that **polls** storage every 1.5s
for rows newer than the last broadcast and pushes typed JSON
(`telemetry` / `health_score` / `alert`) to every connected client.
Polling was a deliberate choice over Postgres LISTEN/NOTIFY, which is
the more elegant fix if this needed to scale past a handful of
concurrent clients or tighter latency - NOTIFY would mean touching
three already-tested writer services for a scale problem this
project doesn't actually have yet. Verified live: a message published
over MQTT showed up on a connected WebSocket client within one poll
interval.

### 4.7 Frontend dashboard — Milestone 6, done
Single-file `frontend/index.html` - React loaded via CDN with Babel
standalone, no build step, so it's just `python -m http.server` or
open-the-file. A plant-floor grid (one card per machine, health bar
and border color-coded green/amber/red - status colors that are a
real, trained-on convention in control rooms, not decoration) and a
live alert feed that tags each entry `maintenance` or `security` by
color. Design direction is deliberately control-room / HMI, not SaaS
dashboard: dark charcoal panels, monospace for every numeric readout
(instrumentation reads as instrumentation), and a slow radar-sweep
gradient behind the whole page as the one signature element, tying
the visual identity straight to what the product is actually called.
Loads initial state from the REST API, then switches to the
WebSocket for live updates; reconnects automatically if the socket
drops.

## 5. Data model

**Telemetry** (`plant/{machine_id}/telemetry`):
```json
{
  "machine_id": "press-01",
  "timestamp": "2026-08-04T10:15:32.123Z",
  "vibration_mm_s": 1.24,
  "temperature_c": 42.1,
  "current_a": 8.53
}
```

**Commands** (`plant/{machine_id}/commands` - now mostly *normal*
traffic with anomalies mixed in, revised in Milestone 4; see
`simulator/machines.py`. Ground truth of which is which is never
published on the wire, only used for the simulator's own console log
and for grading the security rules in tests):
```json
{
  "machine_id": "press-01",
  "timestamp": "2026-08-04T10:15:32.123Z",
  "command": "set_setpoint",
  "value": 178.3,
  "source_ip": "10.0.14.201"
}
```

**Storage schema** — implemented in full, see [`db/schema.sql`](../db/schema.sql)
(applied automatically on first `docker compose up`). Five tables,
each with exactly one writer:

- `telemetry` — written only by the ingestion consumer.
  `PRIMARY KEY (machine_id, time)` is what makes its upserts
  idempotent, not just a good intention.
- `health_scores` — written only by the maintenance detector. Kept
  as its own hypertable rather than a column on `telemetry`
  specifically so two services never write the same row - no
  cross-service UPDATE racing against the ingestion consumer's
  inserts.
- `commands` — written only by the ingestion consumer. Audit trail
  today; also what the security detector's tests replay against.
- `alerts` — written by *both* detectors, split by `alert_type`
  (`maintenance` / `security`) with a `severity` column
  (`info`/`warning`/`critical`). This table, more than the pitch, is
  what makes "one dashboard, two risk types" literally true.
- `dead_letters` — anything failing validation on the way in.
  Confirmed working in testing: a malformed payload and a payload
  missing required fields both landed here correctly, original
  content preserved.

## 6. Production-grade concerns

**Reliability** — MQTT QoS 1 from the simulator, since silently
losing a reading defeats the point of a monitoring system. A
dead-letter path for anything failing schema validation, implemented
and tested. Idempotent storage writes (upsert on `machine_id, time`)
confirmed by republishing an exact duplicate and checking the row
count didn't move. A message-handler crash was found the same way (a
non-JSON payload took the whole ingestion consumer down) and fixed -
see section 4.2. Both detectors reuse the same "one bad message can't
kill the process" wrapper for the same reason.

**Observability** — structured JSON logging throughout every service
past the simulator. Every detector logs the alert it raises, with
enough detail (sensor, deviation, source IP, which rule) to act on
without a database query. `/health` on the API checks the DB
connection, not just that the process is up. **Metrics, done**: every
service exposes a Prometheus `/metrics` endpoint - the API on its own
port (request counts by path/status, connected-WebSocket gauge), and
ingestion, the maintenance detector, and the security detector each
on a dedicated port (9101/9102/9103) since they're otherwise
MQTT-only daemons with no HTTP server of their own. Cross-checked
live against real DB row counts, not just "the counter exists":
a fresh run's `ingestion_messages_total{topic_type="telemetry"}`
matched `maintenance_readings_processed_total` exactly (24 and 24),
and both matched the delta in the `telemetry`/`health_scores` tables.
Tracing remains an honest gap - named as a target, not instrumented.

**Security** — the API's REST routes sit behind an optional API key,
implemented and verified (401/401/200 across no-key/wrong-key/
right-key). The WebSocket route did **not** enforce it at all until
this was caught by grepping for the auth dependency across the file
after the REST routes already looked solid - `/ws` was silently wide
open regardless of `API_KEY`. Fixed via a query param
(`?api_key=...`, since browsers can't set custom headers on a
WebSocket handshake) checked before `accept()`; verified live with
real rejected/accepted connections (HTTP 403 on no-key and
wrong-key, clean connect on the right one) and covered in
`test_websocket_requires_key_when_set`. The dashboard was also
silently broken by this the other way - it never sent a key at all,
so it would have stopped working the moment anyone actually enabled
`API_KEY` - fixed alongside (`window.RADAR_API_KEY`, see README).
Past localhost, the broker needs TLS and real auth; `allow_anonymous
true` in `mosquitto.conf` is dev-only, called out in the file
itself. The elephant in the room, worth naming directly in an
interview rather than glossing over: real OT networks are supposed
to be air-gapped from IT for exactly the reason this project
simulates detecting, and this project necessarily puts simulated OT
traffic on the same network as everything else for practicality.

**Scalability** — TimescaleDB chunk interval tuned to actual
volume - daily chunks are overkill at 4 machines, same knob that
matters at 4,000. The API is stateless and would scale behind a load
balancer for REST; the WebSocket's polling design is the honest
limit here (see section 4.6) - fine at portfolio scale, and the
upgrade path (LISTEN/NOTIFY) is named rather than pretended-around.
Kafka in front of MQTT is documented in full below but deliberately
not built - see the note at the end of this section.

**Testing** — per-component test suites, all passing, all run for
real rather than assumed: `simulator/test_machines.py`,
`detection/test_baseline.py` (includes the multi-seed false-positive
and detection-timing checks that actually drove the CUSUM design),
`detection/test_security_rules.py` (includes the 4,000-command
integration check against the simulator's ground truth), and
`api/test_main.py` (12 tests via FastAPI's TestClient against the
real database, not mocks - REST shape/filter/validation checks, the
`/metrics` endpoint, and both auth paths). Writing the API tests is
what surfaced the WS auth gap below - the REST tests all passed
easily, which is exactly the kind of false confidence a missing test
produces. **CI, done**: `.github/workflows/ci.yml` runs a lean
`unit-tests` job (no infrastructure needed at all - a direct payoff
of keeping detection logic separate from MQTT/DB plumbing throughout)
plus a `live-smoke-test` job that stands up a real Postgres service
container and Mosquitto, runs the API tests against it, then runs all
three pipeline services and the simulator for real and asserts rows
actually land. The exact sequence was validated by hand against a
freshly-recreated database before being committed to the workflow
file, not just written and hoped for. One honest caveat: local
validation ran against plain Postgres, since
this sandbox can't run the TimescaleDB extension - the CI config
itself uses the real `timescale/timescaledb` image, so CI gets full
hypertable behavior this local check couldn't confirm.

**Kafka swap-in — documented, deliberately not built.** The plan
from section 4.2 was always: bridge MQTT into Kafka, point the
consumers at Kafka instead. Checked feasibility before attempting
it: apt has Kafka *client* libraries but no broker package, and the
actual broker distributions (Apache's own dist servers, Confluent's,
Redpanda's) aren't reachable from this sandbox's network allowlist -
only apt mirrors, PyPI, npm, crates.io, and GitHub are. Rather than
either skip this silently or claim a test I can't actually run, here
is the concrete plan, detailed enough to build from:

1. A bridge process subscribes to `plant/+/telemetry` and
   `plant/+/commands` exactly like `ingestion/consumer.py` does
   today, and republishes each message onto a `telemetry` / `commands`
   Kafka topic, partitioned by `machine_id` so each machine's event
   order holds while different machines process in parallel.
2. Because `baseline.py` and `security_rules.py` are already fully
   decoupled from the MQTT plumbing, the swap is confined to the
   `main()` wiring in `consumer.py`, `maintenance_detector.py`, and
   `security_detector.py` - subscribe to a Kafka consumer group
   instead of an MQTT topic. None of the actual detection logic moves.
3. Offsets commit manually after a batch is durably written to
   Postgres, not on Kafka's auto-commit - the direct analog of the
   MQTT+QoS-1 guarantee this repo already relies on.
4. What this actually buys over MQTT-direct: replay (rewind and
   reprocess history - e.g. to backfill `health_scores` after a bug
   fix in `baseline.py`) and horizontal scaling of consumers within a
   group as machine count grows past what one process handles.

Illustrative sketch of the bridge (unbuilt, untested - not present
anywhere else in this repo):
```python
# Sketch only - would sit in front of the current direct-MQTT
# subscription in ingestion/consumer.py.
from confluent_kafka import Producer
import paho.mqtt.client as mqtt

producer = Producer({"bootstrap.servers": "localhost:9092"})

def on_message(client, userdata, msg):
    topic = "telemetry" if msg.topic.endswith("/telemetry") else "commands"
    machine_id = msg.topic.split("/")[1]
    producer.produce(topic, key=machine_id, value=msg.payload)
    producer.poll(0)
```

## 7. Build roadmap

| Milestone | Scope | Status |
|---|---|---|
| M1 | Telemetry simulator + local MQTT broker | **Done** |
| M2 | Ingestion + storage (consumer writing to TimescaleDB) | **Done** |
| M3 | Predictive maintenance detector | **Done** |
| M4 | OT security detector | **Done** |
| M5 | API layer (REST + WebSocket) | **Done** |
| M6 | Dashboard (React, single-file) | **Done** |
| M7 | Production hardening: CI **(done)**, metrics **(done)**, tracing (gap), Kafka swap-in (documented, not built) | Mostly done |

## 8. What this project demonstrates

For a data-engineer-leaning interview, the load-bearing sentence is
some version of: "I built a real-time pipeline that ingests
IIoT-standard telemetry over MQTT and scores it for both equipment
failure risk and OT security anomalies, sharing one schema and one
alert pipeline - and when my first anomaly-detection design didn't
actually catch the failure pattern I built it to catch, I found that
by testing against real data, diagnosed why, and replaced it with
the standard statistical-process-control technique for the problem."
That's stream ingestion, schema design with real idempotency
guarantees, two independently-validated detection strategies (one of
which required genuine iteration, not just a first draft that
happened to work), a storage decision with a stated reason, an API
with real auth, real metrics, and a documented scaling tradeoff, a
working frontend, and a CI pipeline that was validated by hand before
being trusted - considerably more to talk through than "I built a
dashboard." The Kafka section is worth mentioning too, for a
different reason: it's a live example of scoping a piece out
honestly with a real reason, instead of quietly skipping it or
claiming untested work as done.

---
All milestones through M6, plus CI and metrics from M7, have working,
tested code in this repo. Kafka is a documented plan, not code. This
document is living - update the roadmap table as tracing lands.
