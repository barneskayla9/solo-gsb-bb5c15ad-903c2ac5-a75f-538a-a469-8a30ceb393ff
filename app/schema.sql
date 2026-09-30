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
-- ``revision`` starts at 1 and is bumped by every successful reschedule; the
-- ``orig_*`` columns freeze the first-created window so a create-key retry is
-- always judged against — and returns — the original booking, even after the
-- reservation has since been rescheduled.
CREATE TABLE IF NOT EXISTS reservations (
    id              BIGSERIAL PRIMARY KEY,
    point_id        BIGINT NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    start_utc       TIMESTAMPTZ NOT NULL,
    end_utc         TIMESTAMPTZ NOT NULL,
    rules_version   INTEGER NOT NULL,        -- published version validated against
    revision        INTEGER NOT NULL DEFAULT 1,
    orig_start_utc  TIMESTAMPTZ,
    orig_end_utc    TIMESTAMPTZ,
    orig_rules_version INTEGER,
    cancel_token    TEXT NOT NULL,           -- capability token for cancellation
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at    TIMESTAMPTZ,
    UNIQUE (point_id, idempotency_key)
);

-- Migration for databases created before rescheduling existed.
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS orig_start_utc TIMESTAMPTZ;
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS orig_end_utc TIMESTAMPTZ;
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS orig_rules_version INTEGER;
UPDATE reservations
   SET orig_start_utc = start_utc,
       orig_end_utc = end_utc,
       orig_rules_version = rules_version
 WHERE orig_start_utc IS NULL;
ALTER TABLE reservations ALTER COLUMN orig_start_utc SET NOT NULL;
ALTER TABLE reservations ALTER COLUMN orig_end_utc SET NOT NULL;
ALTER TABLE reservations ALTER COLUMN orig_rules_version SET NOT NULL;

-- One row per successful reschedule.  (reservation_id, idempotency_key) is the
-- reschedule idempotency scope: a same-content retry replays the stored
-- successful attempt; the resulting window/revision are frozen here so the
-- replay is identical even after later reschedules or cancellation.
CREATE TABLE IF NOT EXISTS reservation_reschedules (
    id                BIGSERIAL PRIMARY KEY,
    reservation_id    BIGINT NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    point_id          BIGINT NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    idempotency_key   TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,      -- revision the request was based on
    start_utc         TIMESTAMPTZ NOT NULL,
    end_utc           TIMESTAMPTZ NOT NULL,
    rules_version     INTEGER NOT NULL,      -- published version validated against
    resulting_revision INTEGER NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (reservation_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_reschedules_point
    ON reservation_reschedules (point_id);

CREATE INDEX IF NOT EXISTS idx_reservations_point_active
    ON reservations (point_id, start_utc) WHERE status = 'active';
