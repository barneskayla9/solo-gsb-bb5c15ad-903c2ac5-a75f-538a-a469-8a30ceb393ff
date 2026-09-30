"""FastAPI application: recycling-point service calendar HTTP API."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import psycopg

from . import db
from .config import settings
from .errors import ApiError
from .errors import (
    IdempotencyConflictError,
    InvalidPeriodError,
    OutsideOpenHoursError,
    ReservationNotReschedulableError,
    ReservationNotFoundError,
    ReservationOverlapError,
    ReservationRevisionConflictError,
)
from .schemas import (
    CalendarOut,
    DayOut,
    IntervalOut,
    PointCreateIn,
    PointOut,
    PointUpdateIn,
    PublishOut,
    PublishRulesIn,
    ReservationCancelIn,
    ReservationCreateIn,
    ReservationOut,
    ReservationRescheduleIn,
    ReservationStateOut,
    VersionOut,
    VersionSummaryOut,
    utc_iso,
    validate_reservation_window,
    validate_rules_payload,
    validate_timezone,
)
from .scheduler import build_calendar, build_open_intervals, get_zone
from .db import payload_to_ruleset


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(settings.database_url)
    yield
    db.close_pool()


app = FastAPI(
    title="Recycling Point Service Calendar API",
    version="1.0.0",
    description=(
        "Versioned weekly openings, full-day closures and same-day makeup "
        "openings for community recycling points, expanded to UTC intervals."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Error handling — every error is a distinguishable JSON envelope
# ---------------------------------------------------------------------------

@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_request",
                "message": "request payload failed validation",
                # Validator exceptions live in the error context; render them
                # as text so the envelope is always JSON-serializable.
                "details": {
                    "errors": jsonable_encoder(
                        exc.errors(), custom_encoder={Exception: str}
                    )
                },
            }
        },
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------

@app.post("/points", response_model=PointOut, status_code=201, tags=["points"])
def create_point(body: PointCreateIn) -> PointOut:
    validate_timezone(body.timezone)
    with db.conn() as connection:
        with connection.transaction():
            row = db.create_point(connection.cursor(), body.name, body.timezone)
    return PointOut(**row)


@app.get("/points/{point_id}", response_model=PointOut, tags=["points"])
def read_point(point_id: int) -> PointOut:
    with db.conn() as connection:
        row = db.get_point(connection.cursor(), point_id)
    return PointOut(**row)


@app.patch("/points/{point_id}", response_model=PointOut, tags=["points"])
def patch_point(point_id: int, body: PointUpdateIn) -> PointOut:
    fields = body.model_dump(exclude_unset=True)
    if "timezone" in fields:
        validate_timezone(fields["timezone"])
    with db.conn() as connection:
        with connection.transaction():
            cur = connection.cursor()
            if "timezone" in fields:
                # A timezone change re-interprets every wall-clock rule; it is
                # refused when it would un-cover an active reservation.  The
                # point row lock serializes this check against concurrent
                # reservation creations.
                db.lock_point(cur, point_id)
                latest = db.get_latest_payload(cur, point_id)
                if latest is not None:
                    db.assert_reservations_fit(
                        cur, point_id, fields["timezone"], latest["payload"]
                    )
            row = db.update_point(cur, point_id, **fields)
    return PointOut(**row)


# ---------------------------------------------------------------------------
# Versioned rule sets
# ---------------------------------------------------------------------------

@app.post(
    "/points/{point_id}/rules",
    response_model=PublishOut,
    status_code=201,
    tags=["rules"],
)
def publish_rules(point_id: int, body: PublishRulesIn) -> PublishOut:
    # Semantic validation (invalid periods, duplicates) before touching the DB.
    payload = validate_rules_payload(body.rules)
    with db.conn() as connection:
        with connection.transaction():
            row = db.publish_rules(
                connection.cursor(), point_id, body.expected_version, payload
            )
    return PublishOut(
        point_id=point_id, version=row["version"], published_at=row["published_at"]
    )


@app.get(
    "/points/{point_id}/rules",
    response_model=VersionOut,
    tags=["rules"],
)
def read_current_rules(point_id: int) -> VersionOut:
    with db.conn() as connection:
        point, row = db.get_current_rules(connection.cursor(), point_id)
    return VersionOut(
        point_id=point_id,
        version=row["version"],
        published_at=row["published_at"],
        rules=row["payload"],
    )


@app.get(
    "/points/{point_id}/rules/versions",
    response_model=list[VersionSummaryOut],
    tags=["rules"],
)
def list_rule_versions(point_id: int) -> list[VersionSummaryOut]:
    with db.conn() as connection:
        rows = db.list_versions(connection.cursor(), point_id)
    return [VersionSummaryOut(**row) for row in rows]


@app.get(
    "/points/{point_id}/rules/versions/{version}",
    response_model=VersionOut,
    tags=["rules"],
)
def read_rule_version(point_id: int, version: int) -> VersionOut:
    with db.conn() as connection:
        row = db.get_version(connection.cursor(), point_id, version)
    return VersionOut(
        point_id=point_id,
        version=row["version"],
        published_at=row["published_at"],
        rules=row["payload"],
    )


# ---------------------------------------------------------------------------
# Calendar query — always against one published version
# ---------------------------------------------------------------------------

@app.get(
    "/points/{point_id}/calendar",
    response_model=CalendarOut,
    tags=["calendar"],
)
def calendar(
    point_id: int,
    date_from: date = Query(..., alias="from"),
    date_to: date = Query(..., alias="to"),
    version: int | None = Query(
        default=None,
        ge=1,
        description="Pin the query to one published version; defaults to latest.",
    ),
) -> CalendarOut:
    if date_to < date_from:
        raise InvalidPeriodError("'to' must be on or after 'from'")
    span = (date_to - date_from).days + 1
    if span > settings.max_query_days:
        raise InvalidPeriodError(
            f"date range spans {span} days; maximum is {settings.max_query_days}",
            details={"max_query_days": settings.max_query_days, "span_days": span},
        )
    with db.conn() as connection:
        # Repeatable-read snapshot: the point row and the loaded rule version
        # always agree, even if a concurrent publish commits mid-query.
        previous_level = connection.isolation_level
        connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        try:
            with connection.transaction():
                cur = connection.cursor()
                if version is None:
                    point, row = db.get_current_rules(cur, point_id)
                else:
                    point, row = db.get_pinned_rules(cur, point_id, version)
        finally:
            connection.isolation_level = previous_level
    used_version = row["version"]
    ruleset = payload_to_ruleset(row["payload"])
    days = build_calendar(point["timezone"], ruleset, date_from, date_to)
    return CalendarOut(
        point_id=point_id,
        version=used_version,
        timezone=point["timezone"],
        date_from=date_from,
        date_to=date_to,
        days=[
            DayOut(
                local_date=d.local_date,
                intervals=[
                    IntervalOut(
                        start=utc_iso(start),
                        end=utc_iso(end),
                        sources=[s.as_dict() for s in sources],
                    )
                    for start, end, sources in d.intervals
                ],
            )
            for d in days
        ],
    )


# ---------------------------------------------------------------------------
# Reservations — booked against the currently published calendar
# ---------------------------------------------------------------------------

def _reservation_out(
    row: dict,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    rules_version: int | None = None,
    revision: int | None = None,
) -> ReservationOut:
    return ReservationOut(
        id=row["id"],
        point_id=row["point_id"],
        status=row["status"],
        start=utc_iso(start if start is not None else row["start_utc"]),
        end=utc_iso(end if end is not None else row["end_utc"]),
        rules_version=(
            rules_version if rules_version is not None else row["rules_version"]
        ),
        revision=revision if revision is not None else row["revision"],
        cancel_token=row["cancel_token"],
        created_at=row["created_at"],
        cancelled_at=row["cancelled_at"],
    )


def _reservation_state_out(row: dict) -> ReservationStateOut:
    return ReservationStateOut(
        id=row["id"],
        point_id=row["point_id"],
        status=row["status"],
        start=utc_iso(row["start_utc"]),
        end=utc_iso(row["end_utc"]),
        rules_version=row["rules_version"],
        revision=row["revision"],
        created_at=row["created_at"],
        cancelled_at=row["cancelled_at"],
    )


def _validate_bookable_window(
    cur,
    point: dict,
    start: datetime,
    end: datetime,
    now: datetime,
    *,
    exclude_reservation_id: int | None = None,
) -> int:
    """Booking-policy + calendar-containment + overlap checks.

    Shared by creation and rescheduling; both must obey the same rules.
    ``exclude_reservation_id`` makes a rescheduling reservation ignore its
    own still-active row when checking overlaps.  The caller holds the point
    row lock, so the published version and the overlap set are stable.

    Returns the currently published rules version that was validated against.
    """
    validate_reservation_window(
        start,
        end,
        now,
        max_days_ahead=settings.reservation_max_days_ahead,
        max_duration=timedelta(
            minutes=settings.reservation_max_duration_minutes
        ),
    )

    # The reservation must fit inside one continuous open interval of the
    # currently published calendar (intervals may cross local midnight; they
    # are not clipped per day here).
    _, rules_row = db.get_current_rules(cur, point["id"])
    rules_version = rules_row["version"]
    zone = get_zone(point["timezone"])
    ruleset = payload_to_ruleset(rules_row["payload"])
    date_from = (start - timedelta(days=1)).astimezone(zone).date()
    date_to = (end + timedelta(days=1)).astimezone(zone).date()
    intervals = build_open_intervals(
        point["timezone"], ruleset, date_from, date_to
    )
    if not any(s <= start and end <= e for s, e in intervals):
        raise OutsideOpenHoursError(
            "reservation is not fully contained in one continuous "
            "open interval of the published calendar",
            details={
                "start": utc_iso(start),
                "end": utc_iso(end),
                "rules_version": rules_version,
            },
        )

    overlapping = db.find_overlapping(
        cur, point["id"], start, end, exclude_reservation_id
    )
    if overlapping:
        raise ReservationOverlapError(
            "reservation overlaps an existing active reservation",
            details={
                "conflicting_reservation_ids": [r["id"] for r in overlapping]
            },
        )
    return rules_version


@app.post(
    "/points/{point_id}/reservations",
    response_model=ReservationOut,
    status_code=201,
    tags=["reservations"],
)
def create_reservation(
    point_id: int, body: ReservationCreateIn, response: Response
) -> ReservationOut:
    start = body.start.astimezone(timezone.utc)
    end = body.end.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    with db.conn() as connection:
        with connection.transaction():
            cur = connection.cursor()
            # The point row lock serializes creations against each other and
            # against rule publishes / timezone changes for this point.
            point = db.lock_point(cur, point_id)

            # Idempotency: the first successful request wins.  A same-content
            # retry replays its stored result (even if it has since been
            # cancelled or rescheduled); different content under the same key
            # conflicts.  Comparison and the replayed window both use the
            # frozen first-creation content, so later reschedules never
            # change the meaning of the original create key.
            existing = db.get_reservation_by_key(
                cur, point_id, body.idempotency_key
            )
            if existing is not None:
                if (
                    existing["orig_start_utc"] == start
                    and existing["orig_end_utc"] == end
                ):
                    response.status_code = 200
                    return _reservation_out(
                        existing,
                        start=existing["orig_start_utc"],
                        end=existing["orig_end_utc"],
                        rules_version=existing["orig_rules_version"],
                    )
                raise IdempotencyConflictError(
                    f"idempotency key {body.idempotency_key!r} was already used "
                    f"on point {point_id} with a different time window",
                    details={
                        "reservation_id": existing["id"],
                        "start": utc_iso(existing["orig_start_utc"]),
                        "end": utc_iso(existing["orig_end_utc"]),
                    },
                )

            rules_version = _validate_bookable_window(cur, point, start, end, now)

            row = db.insert_reservation(
                cur,
                point_id,
                body.idempotency_key,
                start,
                end,
                rules_version,
            )
    return _reservation_out(row)


@app.post(
    "/points/{point_id}/reservations/{reservation_id}/cancel",
    response_model=ReservationStateOut,
    tags=["reservations"],
)
def cancel_reservation(
    point_id: int, reservation_id: int, body: ReservationCancelIn
) -> ReservationStateOut:
    with db.conn() as connection:
        with connection.transaction():
            row = db.cancel_reservation(
                connection.cursor(), point_id, reservation_id, body.cancel_token
            )
    return _reservation_state_out(row)


@app.post(
    "/points/{point_id}/reservations/{reservation_id}/reschedule",
    response_model=ReservationOut,
    tags=["reservations"],
)
def reschedule_reservation(
    point_id: int, reservation_id: int, body: ReservationRescheduleIn
) -> ReservationOut:
    """Reschedule an active, not-yet-started reservation with its cancel token.

    Locking order (reservation row, then point row) matches cancellation and
    every point-wide mutation, so reschedules serialize against cancels,
    creations, rule publishes and timezone changes with no deadlock window.
    Every check and the row update happen in one transaction: any failure
    rolls back and leaves the original slot occupied.
    """
    start = body.start.astimezone(timezone.utc)
    end = body.end.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    with db.conn() as connection:
        with connection.transaction():
            cur = connection.cursor()

            # Identity and capability first: 404 / 403 share cancel's
            # semantics, and the reservation row lock serializes against
            # concurrent cancellation and other reschedules.
            reservation = db.get_reservation_for_update(
                cur, point_id, reservation_id
            )
            if reservation is None:
                raise ReservationNotFoundError(
                    f"reservation {reservation_id} of point {point_id} does not exist",
                    details={
                        "point_id": point_id,
                        "reservation_id": reservation_id,
                    },
                )
            db.assert_cancel_token(reservation, body.cancel_token)

            # Reschedule idempotency replay: a same-content retry of a
            # successful attempt returns that attempt's result.  A stale
            # expected revision is still a revision conflict; a different
            # payload under the same key is an idempotency conflict.
            attempt = db.get_reschedule_by_key(
                cur, reservation_id, body.idempotency_key
            )
            if attempt is not None:
                return _reschedule_replay(
                    reservation,
                    attempt,
                    expected_revision=body.expected_revision,
                    start=start,
                    end=end,
                )

            if reservation["status"] == "cancelled":
                raise ReservationNotReschedulableError(
                    "reservation has been cancelled and cannot be rescheduled",
                    details={
                        "reservation_id": reservation_id,
                        "status": "cancelled",
                    },
                )
            if reservation["start_utc"] <= now:
                raise ReservationNotReschedulableError(
                    "reservation has already started and cannot be rescheduled",
                    details={
                        "reservation_id": reservation_id,
                        "start": utc_iso(reservation["start_utc"]),
                        "now": utc_iso(now),
                    },
                )
            if reservation["revision"] != body.expected_revision:
                raise ReservationRevisionConflictError(
                    f"expected revision {body.expected_revision} but reservation "
                    f"{reservation_id} is at revision {reservation['revision']}",
                    details={
                        "reservation_id": reservation_id,
                        "expected_revision": body.expected_revision,
                        "current_revision": reservation["revision"],
                    },
                )

            point = db.lock_point(cur, point_id)
            rules_version = _validate_bookable_window(
                cur,
                point,
                start,
                end,
                now,
                exclude_reservation_id=reservation_id,
            )

            row, _attempt = db.apply_reschedule(
                cur,
                reservation,
                body.idempotency_key,
                body.expected_revision,
                start,
                end,
                rules_version,
            )
    return _reservation_out(row)


def _reschedule_replay(
    reservation: dict,
    attempt: dict,
    *,
    expected_revision: int,
    start: datetime,
    end: datetime,
) -> ReservationOut:
    """Resolve a retried reschedule request against a stored successful one."""
    if attempt["start_utc"] != start or attempt["end_utc"] != end:
        if expected_revision != reservation["revision"]:
            raise ReservationRevisionConflictError(
                f"expected revision {expected_revision} but reservation "
                f"{reservation['id']} is at revision {reservation['revision']}",
                details={
                    "reservation_id": reservation["id"],
                    "expected_revision": expected_revision,
                    "current_revision": reservation["revision"],
                },
            )
        raise IdempotencyConflictError(
            f"reschedule idempotency key {attempt['idempotency_key']!r} was "
            f"already used on reservation {reservation['id']} with a different "
            f"time window",
            details={
                "reservation_id": reservation["id"],
                "start": utc_iso(attempt["start_utc"]),
                "end": utc_iso(attempt["end_utc"]),
            },
        )
    # Same content: the successful attempt's window, rules version and
    # resulting revision are authoritative for the replay; the lifecycle
    # status is the current one (it may have been rescheduled again or
    # cancelled since).
    return _reservation_out(
        reservation,
        start=attempt["start_utc"],
        end=attempt["end_utc"],
        rules_version=attempt["rules_version"],
        revision=attempt["resulting_revision"],
    )
