-- OT/ICS Risk Radar — schema
-- Applied automatically on first container start via docker-compose
-- (mounted into /docker-entrypoint-initdb.d/ on the timescaledb image).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Raw telemetry. Owned solely by the ingestion consumer.
CREATE TABLE IF NOT EXISTS telemetry (
    time            TIMESTAMPTZ NOT NULL,
    machine_id      TEXT NOT NULL,
    vibration_mm_s  DOUBLE PRECISION,
    temperature_c   DOUBLE PRECISION,
    current_a       DOUBLE PRECISION,
    PRIMARY KEY (machine_id, time)
);
SELECT create_hypertable('telemetry', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS telemetry_machine_time_idx ON telemetry (machine_id, time DESC);

-- Continuous health score, owned solely by the predictive-maintenance
-- detector (Milestone 3). Kept as its own hypertable rather than a
-- column on telemetry so each service has exactly one writer and one
-- table it's responsible for - no cross-service UPDATEs racing against
-- the ingestion consumer's inserts.
CREATE TABLE IF NOT EXISTS health_scores (
    time          TIMESTAMPTZ NOT NULL,
    machine_id    TEXT NOT NULL,
    health_score  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (machine_id, time)
);
SELECT create_hypertable('health_scores', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS health_scores_machine_time_idx ON health_scores (machine_id, time DESC);

-- Raw command events — audit trail today, input to the OT security
-- detector (Milestone 4) once it exists.
CREATE TABLE IF NOT EXISTS commands (
    time         TIMESTAMPTZ NOT NULL,
    machine_id   TEXT NOT NULL,
    command      TEXT NOT NULL,
    value        DOUBLE PRECISION,
    source_ip    TEXT,
    raw_payload  JSONB,
    PRIMARY KEY (machine_id, time)
);
SELECT create_hypertable('commands', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS commands_machine_time_idx ON commands (machine_id, time DESC);

-- Alerts from BOTH detectors share one table, split by alert_type —
-- this is what makes "one dashboard, two risk types" true at the
-- data layer, not just in the pitch. Populated starting Milestone 3.
CREATE TABLE IF NOT EXISTS alerts (
    id            BIGSERIAL PRIMARY KEY,
    machine_id    TEXT NOT NULL,
    alert_type    TEXT NOT NULL CHECK (alert_type IN ('maintenance', 'security')),
    severity      TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    description   TEXT NOT NULL,
    raw_payload   JSONB
);
CREATE INDEX IF NOT EXISTS alerts_machine_idx ON alerts (machine_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS alerts_type_idx ON alerts (alert_type, detected_at DESC);

-- Anything that fails schema validation on the way in lands here
-- instead of being silently dropped or taking down the consumer.
CREATE TABLE IF NOT EXISTS dead_letters (
    id           BIGSERIAL PRIMARY KEY,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    topic        TEXT NOT NULL,
    reason       TEXT NOT NULL,
    raw_payload  TEXT
);
