-- Schema for the recycling-point service calendar.
-- Executed once at application startup (idempotent).

CREATE TABLE IF NOT EXISTS points (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    timezone    TEXT NOT NULL,                 -- IANA tz database name
    version     INTEGER NOT NULL DEFAULT 0,    -- number of published versions
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per *published* rule set.  Version 0 means "no rules yet" and is
-- never present; the point's current version always points at the newest row.
CREATE TABLE IF NOT EXISTS rule_versions (
    id          BIGSERIAL PRIMARY KEY,
    point_id    BIGINT NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    payload     JSONB NOT NULL,                -- {weekly, closed_dates, makeup}
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (point_id, version)
);

CREATE INDEX IF NOT EXISTS idx_rule_versions_point
    ON rule_versions (point_id, version DESC);

-- Visit reservations.  (point_id, idempotency_key) is the client's idempotency
-- scope: the first successful request wins, same-content retries replay it.
CREATE TABLE IF NOT EXISTS reservations (
    id              BIGSERIAL PRIMARY KEY,
    point_id        BIGINT NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    start_utc       TIMESTAMPTZ NOT NULL,
    end_utc         TIMESTAMPTZ NOT NULL,
    rules_version   INTEGER NOT NULL,        -- published version validated against
    cancel_token    TEXT NOT NULL,           -- capability token for cancellation
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at    TIMESTAMPTZ,
    UNIQUE (point_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_reservations_point_active
    ON reservations (point_id, start_utc) WHERE status = 'active';
