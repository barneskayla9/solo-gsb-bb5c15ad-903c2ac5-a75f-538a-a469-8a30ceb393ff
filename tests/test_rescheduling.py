"""End-to-end tests for rescheduling reservations with the cancel token."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from app import db

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers (mirroring test_reservations.py)
# ---------------------------------------------------------------------------

def create_point(client, tz="UTC"):
    r = client.post("/points", json={"name": "改期测试点", "timezone": tz})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def tomorrow_at(hour, minute=0):
    day = (datetime.now(UTC) + timedelta(days=1)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def open_point(client, weekly=None, tz="UTC"):
    pid = create_point(client, tz=tz)
    rules = {
        "weekly": weekly
        if weekly is not None
        else [
            {
                "weekday": tomorrow_at(0).isoweekday(),
                "start_time": "08:00",
                "end_time": "12:00",
            }
        ],
        "closed_dates": [],
        "makeup": [],
    }
    r = client.post(
        f"/points/{pid}/rules",
        json={"expected_version": 0, "rules": rules},
    )
    assert r.status_code == 201, r.text
    return pid


def reserve(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/reservations",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def reschedule(client, pid, rid, body):
    if isinstance(body.get("start"), datetime):
        body = {**body, "start": iso(body["start"]), "end": iso(body["end"])}
    return client.post(
        f"/points/{pid}/reservations/{rid}/reschedule", json=body
    )


def reschedule_body(reservation, start, end, key, expected=None):
    return {
        "cancel_token": reservation["cancel_token"],
        "expected_revision": reservation["revision"] if expected is None else expected,
        "idempotency_key": key,
        "start": start,
        "end": end,
    }


def book(client, pid, key="orig", start=None, end=None):
    start = start or tomorrow_at(9)
    end = end or tomorrow_at(10)
    r = reserve(client, pid, key, start, end)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------

def test_reschedule_success_keeps_id_token_bumps_revision(client):
    pid = open_point(client)
    created = book(client, pid)
    rid = created["id"]

    r = reschedule(
        client, pid, rid,
        reschedule_body(created, tomorrow_at(11), tomorrow_at(11, 30), "move-1"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == rid  # reservation number preserved
    assert body["cancel_token"] == created["cancel_token"]  # token preserved
    assert body["status"] == "active"
    assert body["start"] == iso(tomorrow_at(11))
    assert body["end"] == iso(tomorrow_at(11, 30))
    assert body["rules_version"] == 1
    assert body["revision"] == 2
    assert body["cancelled_at"] is None


def test_repeated_reschedules_keep_bumping_revision(client):
    pid = open_point(client)
    cur = book(client, pid)
    cur = reschedule(
        client, pid, cur["id"],
        reschedule_body(cur, tomorrow_at(8), tomorrow_at(8, 30), "move-1"),
    ).json()
    assert cur["revision"] == 2
    cur = reschedule(
        client, pid, cur["id"],
        reschedule_body(cur, tomorrow_at(11), tomorrow_at(12), "move-2"),
    ).json()
    assert cur["revision"] == 3
    assert cur["start"] == iso(tomorrow_at(11))
    assert cur["end"] == iso(tomorrow_at(12))


def test_reschedule_updates_rules_version_basis(client):
    pid = open_point(client)
    created = book(client, pid)
    weekday = tomorrow_at(0).isoweekday()
    # Publish a still-covering v2 (07:00-13:00); the reschedule then records v2.
    r = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 1,
            "rules": {
                "weekly": [
                    {"weekday": weekday, "start_time": "07:00", "end_time": "13:00"}
                ],
                "closed_dates": [],
                "makeup": [],
            },
        },
    )
    assert r.status_code == 201, r.text
    r = reschedule(
        client, pid, created["id"],
        reschedule_body(created, tomorrow_at(11), tomorrow_at(12), "move-v2"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["rules_version"] == 2
    assert r.json()["revision"] == 2


# ---------------------------------------------------------------------------
# Reschedule idempotency
# ---------------------------------------------------------------------------

def test_same_reschedule_key_same_content_replays_successful_result(client):
    pid = open_point(client)
    created = book(client, pid)
    body = reschedule_body(created, tomorrow_at(11), tomorrow_at(12), "idem")
    first = reschedule(client, pid, created["id"], body)
    assert first.status_code == 200, first.text

    # Even after the reservation moves again, the old key replays the first
    # successful reschedule (revision 2, old window), not the current state.
    moved_again = reschedule(
        client, pid, created["id"],
        reschedule_body(first.json(), tomorrow_at(8), tomorrow_at(9), "again"),
    )
    assert moved_again.status_code == 200, moved_again.text
    assert moved_again.json()["revision"] == 3

    replay = reschedule(client, pid, created["id"], body)
    assert replay.status_code == 200, replay.text
    replayed = replay.json()
    assert replayed["start"] == iso(tomorrow_at(11))
    assert replayed["end"] == iso(tomorrow_at(12))
    assert replayed["revision"] == 2
    assert replayed["rules_version"] == 1
    assert replayed["cancel_token"] == created["cancel_token"]
    assert replayed["id"] == created["id"]


def test_same_reschedule_key_different_content_conflicts(client):
    pid = open_point(client)
    created = book(client, pid)
    body1 = reschedule_body(created, tomorrow_at(11), tomorrow_at(12), "k")
    first = reschedule(client, pid, created["id"], body1)
    assert first.status_code == 200

    # Client now knows revision 2; reusing the key with a different window and
    # the current revision is an idempotency conflict (not a stale revision).
    body2 = {
        **body1,
        "expected_revision": 2,
        "start": iso(tomorrow_at(8)),
        "end": iso(tomorrow_at(9)),
    }
    r = reschedule(client, pid, created["id"], body2)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"
    # The stored attempt is untouched.
    replay = reschedule(client, pid, created["id"], body1)
    assert replay.json()["start"] == iso(tomorrow_at(11))


def test_same_reschedule_key_replay_with_stale_revision_is_revision_conflict(client):
    # A different-content call using a stale expected revision reports the
    # revision conflict, even though the key is already known.
    pid = open_point(client)
    created = book(client, pid)
    body1 = reschedule_body(created, tomorrow_at(11), tomorrow_at(12), "k")
    assert reschedule(client, pid, created["id"], body1).status_code == 200

    body2 = {
        **body1,
        "expected_revision": 1,  # stale; reservation is now at revision 2
        "start": iso(tomorrow_at(8)),
        "end": iso(tomorrow_at(9)),
    }
    r = reschedule(client, pid, created["id"], body2)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_revision_conflict"
    assert err["details"]["expected_revision"] == 1
    assert err["details"]["current_revision"] == 2


def test_same_content_replay_is_accepted_even_with_old_expected_revision(client):
    # A pure retry (identical payload) is an idempotent replay regardless of
    # the expected_revision value sent on the retry.
    pid = open_point(client)
    created = book(client, pid)
    body = reschedule_body(created, tomorrow_at(11), tomorrow_at(12), "k")
    assert reschedule(client, pid, created["id"], body).status_code == 200

    retry = {**body, "expected_revision": 1}
    r = reschedule(client, pid, created["id"], retry)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2


def test_same_content_replay_after_cancel_returns_cancelled_state(client):
    pid = open_point(client)
    created = book(client, pid)
    body = reschedule_body(created, tomorrow_at(11), tomorrow_at(12), "k")
    assert reschedule(client, pid, created["id"], body).status_code == 200
    cancel = client.post(
        f"/points/{pid}/reservations/{created['id']}/cancel",
        json={"cancel_token": created["cancel_token"]},
    )
    assert cancel.status_code == 200
    # The successful move still replays; the lifecycle status follows the row.
    replay = reschedule(client, pid, created["id"], body)
    assert replay.status_code == 200
    replayed = replay.json()
    assert replayed["status"] == "cancelled"
    assert replayed["cancelled_at"] is not None
    assert replayed["start"] == iso(tomorrow_at(11))
    assert replayed["revision"] == 2


def test_stale_expected_revision_conflicts(client):
    pid = open_point(client)
    created = book(client, pid)
    stale = reschedule_body(
        created, tomorrow_at(11), tomorrow_at(12), "stale-key", expected=99
    )
    r = reschedule(client, pid, created["id"], stale)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_revision_conflict"
    assert err["details"]["expected_revision"] == 99
    assert err["details"]["current_revision"] == 1


def test_reschedule_keys_are_scoped_per_reservation(client):
    pid = open_point(client)
    a = book(client, pid, "a", tomorrow_at(9), tomorrow_at(10))
    b = book(client, pid, "b", tomorrow_at(10), tomorrow_at(11))
    ra = reschedule(
        client, pid, a["id"],
        reschedule_body(a, tomorrow_at(8), tomorrow_at(8, 30), "shared-key"),
    )
    rb = reschedule(
        client, pid, b["id"],
        reschedule_body(b, tomorrow_at(11), tomorrow_at(11, 30), "shared-key"),
    )
    assert ra.status_code == 200, ra.text
    assert rb.status_code == 200, rb.text


# ---------------------------------------------------------------------------
# Cancel-token / identity semantics (same as the cancel endpoint)
# ---------------------------------------------------------------------------

def test_reschedule_unknown_reservation_is_404(client):
    pid = open_point(client)
    r = reschedule(
        client, pid, 999999,
        {
            "cancel_token": "whatever",
            "expected_revision": 1,
            "idempotency_key": "x",
            "start": iso(tomorrow_at(11)),
            "end": iso(tomorrow_at(12)),
        },
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "reservation_not_found"


def test_reschedule_under_wrong_point_is_404(client):
    p1, p2 = open_point(client), open_point(client)
    created = book(client, p1)
    r = reschedule(client, p2, created["id"], reschedule_body(created, tomorrow_at(11), tomorrow_at(12), "x"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "reservation_not_found"


def test_reschedule_requires_and_checks_token(client):
    pid = open_point(client)
    created = book(client, pid)
    base = {
        "expected_revision": 1,
        "idempotency_key": "need-token",
        "start": iso(tomorrow_at(11)),
        "end": iso(tomorrow_at(12)),
    }
    r = reschedule(client, pid, created["id"], {**base, "cancel_token": "wrong"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_cancel_token"
    # missing token -> request validation error on the body shape, but an
    # explicit null/empty token follows the invalid_request semantics.
    r = client.post(
        f"/points/{pid}/reservations/{created['id']}/reschedule",
        json={**base, "cancel_token": None},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# Non-reschedulable states
# ---------------------------------------------------------------------------

def test_cancelled_reservation_cannot_be_rescheduled(client):
    pid = open_point(client)
    created = book(client, pid)
    r = client.post(
        f"/points/{pid}/reservations/{created['id']}/cancel",
        json={"cancel_token": created["cancel_token"]},
    )
    assert r.status_code == 200
    r = reschedule(
        client, pid, created["id"],
        reschedule_body(created, tomorrow_at(11), tomorrow_at(12), "after-cancel"),
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_not_reschedulable"
    assert "cancel" in err["message"]


def test_started_reservation_cannot_be_rescheduled(client):
    pid = open_point(client)
    created = book(client, pid)
    # Force the reservation one hour into the past via the real database.
    with db.conn() as connection:
        with connection.transaction():
            connection.execute(
                "UPDATE reservations SET start_utc = now() - interval '1 hour', "
                "end_utc = now() + interval '1 hour' WHERE id = %s",
                (created["id"],),
            )
    r = reschedule(
        client, pid, created["id"],
        reschedule_body(
            created, tomorrow_at(11), tomorrow_at(12), "too-late", expected=1
        ),
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_not_reschedulable"
    assert "already started" in err["message"]


# ---------------------------------------------------------------------------
# Window / calendar / overlap validation (same rules as creation)
# ---------------------------------------------------------------------------

def test_reschedule_window_policy_failures_are_invalid_period(client):
    pid = open_point(client)
    created = book(client, pid)
    # zero length
    r = reschedule(
        client, pid, created["id"],
        reschedule_body(created, tomorrow_at(11), tomorrow_at(11), "w1"),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # longer than the 2h maximum
    r = reschedule(
        client, pid, created["id"],
        reschedule_body(created, tomorrow_at(8), tomorrow_at(10, 1), "w2"),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # start in the past
    past = datetime.now(UTC) - timedelta(minutes=5)
    r = reschedule(
        client, pid, created["id"],
        reschedule_body(created, past, past + timedelta(minutes=30), "w3"),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # beyond the 30-day horizon
    far = datetime.now(UTC) + timedelta(days=31)
    r = reschedule(
        client, pid, created["id"],
        reschedule_body(created, far, far + timedelta(hours=1), "w4"),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"


def test_reschedule_outside_open_hours_rejected(client):
    pid = open_point(client)
    created = book(client, pid)
    r = reschedule(
        client, pid, created["id"],
        reschedule_body(created, tomorrow_at(13), tomorrow_at(14), "closed"),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "outside_open_hours"


def test_reschedule_overlap_ignores_self_but_blocks_others(client):
    pid = open_point(client)
    me = book(client, pid, "me", tomorrow_at(9), tomorrow_at(10))
    other = book(client, pid, "other", tomorrow_at(10), tomorrow_at(11))
    # Moving onto my own exact current window is a no-op: self is ignored.
    r = reschedule(
        client, pid, me["id"],
        reschedule_body(me, tomorrow_at(9), tomorrow_at(10), "self"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2
    # Overlapping the other reservation is rejected.
    r = reschedule(
        client, pid, me["id"],
        reschedule_body(
            {**me, "revision": 2}, tomorrow_at(10, 30), tomorrow_at(11, 30), "hit"
        ),
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_overlap"
    assert err["details"]["conflicting_reservation_ids"] == [other["id"]]
    # Touching end-to-start is allowed.
    r = reschedule(
        client, pid, me["id"],
        reschedule_body(
            {**me, "revision": 2}, tomorrow_at(11), tomorrow_at(12), "touch"
        ),
    )
    assert r.status_code == 200, r.text


def test_failed_reschedule_does_not_release_or_move_the_original_slot(client):
    pid = open_point(client)
    me = book(client, pid, "me", tomorrow_at(9), tomorrow_at(10))
    # A third party can never take 09:00-10:00 while the failed move holds it.
    assert (
        reserve(client, pid, "squatter", tomorrow_at(9), tomorrow_at(10)).status_code
        == 409
    )
    bad = reschedule(
        client, pid, me["id"],
        reschedule_body(me, tomorrow_at(13), tomorrow_at(14), "bad-move"),
    )
    assert bad.status_code == 409
    # The original slot is still occupied by the same reservation ...
    assert (
        reserve(client, pid, "squatter-2", tomorrow_at(9), tomorrow_at(10)).status_code
        == 409
    )
    # ... and its window/revision are unchanged.
    r = client.post(
        f"/points/{pid}/reservations/{me['id']}/cancel",
        json={"cancel_token": me["cancel_token"]},
    )
    assert r.status_code == 200
    assert r.json()["start"] == iso(tomorrow_at(9))
    assert r.json()["end"] == iso(tomorrow_at(10))
    assert r.json()["revision"] == 1


# ---------------------------------------------------------------------------
# Interaction with the original create idempotency key
# ---------------------------------------------------------------------------

def test_create_key_replay_after_reschedule_returns_original_window(client):
    pid = open_point(client)
    start, end = tomorrow_at(9), tomorrow_at(10)
    created = reserve(client, pid, "create-key", start, end).json()
    moved = reschedule(
        client, pid, created["id"],
        reschedule_body(created, tomorrow_at(11), tomorrow_at(12), "move"),
    ).json()
    assert moved["revision"] == 2

    # The original create key is still judged against the FIRST content:
    # same content replays the original window and token (current revision).
    replay = reserve(client, pid, "create-key", start, end)
    assert replay.status_code == 200
    body = replay.json()
    assert body["id"] == created["id"]
    assert body["start"] == iso(start)
    assert body["end"] == iso(end)
    assert body["cancel_token"] == created["cancel_token"]
    assert body["revision"] == 2
    # The live reservation still occupies the new window.
    assert reserve(client, pid, "check-new", tomorrow_at(11), tomorrow_at(12)).status_code == 409

    # Different content under the original key still conflicts, reporting the
    # original first-created window.
    conflict = reserve(client, pid, "create-key", tomorrow_at(8), tomorrow_at(8, 30))
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "idempotency_conflict"
    assert err["details"]["start"] == iso(start)


def test_rescheduling_frees_the_old_slot(client):
    pid = open_point(client)
    created = book(client, pid, "moving", tomorrow_at(9), tomorrow_at(10))
    r = reschedule(
        client, pid, created["id"],
        reschedule_body(created, tomorrow_at(11), tomorrow_at(12), "away"),
    )
    assert r.status_code == 200
    again = reserve(client, pid, "reuse-old", tomorrow_at(9), tomorrow_at(10))
    assert again.status_code == 201, again.text


# ---------------------------------------------------------------------------
# Timezone-aware input
# ---------------------------------------------------------------------------

def test_reschedule_naive_timestamps_rejected(client):
    pid = open_point(client)
    created = book(client, pid)
    r = client.post(
        f"/points/{pid}/reservations/{created['id']}/reschedule",
        json={
            "cancel_token": created["cancel_token"],
            "expected_revision": 1,
            "idempotency_key": "naive",
            "start": "2026-10-01T11:00:00",
            "end": "2026-10-01T12:00:00",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_reschedule_accepts_other_timezone_offsets(client):
    # 11:00+02:00 == 09:00Z, inside the UTC point's 08:00-12:00 window.
    pid = open_point(client)
    created = book(client, pid)
    r = client.post(
        f"/points/{pid}/reservations/{created['id']}/reschedule",
        json={
            "cancel_token": created["cancel_token"],
            "expected_revision": 1,
            "idempotency_key": "offset",
            "start": iso(tomorrow_at(9)).replace("Z", "+00:00"),
            "end": iso(tomorrow_at(9, 30)).replace("Z", "+00:00"),
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["start"] == iso(tomorrow_at(9))


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_reschedule_and_cancel_exactly_one_wins(client):
    pid = open_point(client)
    created = book(client, pid)
    barrier = threading.Barrier(2)
    outcomes: dict[str, tuple[int, dict]] = {}

    def do_reschedule():
        barrier.wait()
        r = reschedule(
            client, pid, created["id"],
            reschedule_body(created, tomorrow_at(11), tomorrow_at(12), "race-move"),
        )
        outcomes["reschedule"] = (r.status_code, r.json())

    def do_cancel():
        barrier.wait()
        r = client.post(
            f"/points/{pid}/reservations/{created['id']}/cancel",
            json={"cancel_token": created["cancel_token"]},
        )
        outcomes["cancel"] = (r.status_code, r.json())

    threads = [threading.Thread(target=do_reschedule), threading.Thread(target=do_cancel)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    move_status, move_body = outcomes["reschedule"]
    cancel_status, _ = outcomes["cancel"]
    if move_status == 200:
        assert cancel_status == 200  # cancel then sees the moved, active row
        assert move_body["revision"] == 2
    else:
        # Cancel committed first: the reschedule is refused as not reschedulable.
        assert move_status == 409
        assert move_body["error"]["code"] == "reservation_not_reschedulable"
        assert cancel_status == 200


def test_concurrent_reschedules_serialize_with_revision_conflict(client):
    pid = open_point(client)
    created = book(client, pid)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, dict]] = []

    def attempt(key, hour):
        barrier.wait()
        body = reschedule_body(
            created, tomorrow_at(hour), tomorrow_at(hour, 30), key
        )
        r = reschedule(client, pid, created["id"], body)
        outcomes.append((r.status_code, r.json()))

    threads = [
        threading.Thread(target=attempt, args=("race-1", 8)),
        threading.Thread(target=attempt, args=("race-2", 11)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(s for s, _ in outcomes)
    assert statuses == [200, 409]
    loser = next(b for s, b in outcomes if s == 409)
    assert loser["error"]["code"] == "reservation_revision_conflict"


def test_concurrent_reschedule_and_breaking_publish_exactly_one_wins(client):
    pid = open_point(client)
    created = book(client, pid, "race", tomorrow_at(9), tomorrow_at(10))
    barrier = threading.Barrier(2)
    outcomes: dict[str, tuple[int, dict]] = {}

    def do_reschedule():
        barrier.wait()
        r = reschedule(
            client, pid, created["id"],
            reschedule_body(created, tomorrow_at(11), tomorrow_at(12), "race-move"),
        )
        outcomes["reschedule"] = (r.status_code, r.json())

    def do_publish():
        barrier.wait()
        r = client.post(
            f"/points/{pid}/rules",
            json={
                "expected_version": 1,
                "rules": {"weekly": [], "closed_dates": [], "makeup": []},
            },
        )
        outcomes["publish"] = (r.status_code, r.json())

    threads = [threading.Thread(target=do_reschedule), threading.Thread(target=do_publish)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    move_status, move_body = outcomes["reschedule"]
    publish_status, publish_body = outcomes["publish"]
    version = client.get(f"/points/{pid}").json()["version"]
    if move_status == 200:
        # Move committed (while still at v1); the move leaves an active
        # reservation, so emptying the calendar must be refused.
        assert publish_status == 409
        assert publish_body["error"]["code"] == "reservation_change_conflict"
        assert version == 1
    else:
        # Publish committed first; the reschedule finds no open interval.
        assert move_status == 409
        assert move_body["error"]["code"] == "outside_open_hours"
        assert publish_status == 201
        assert version == 2


def test_concurrent_reschedule_into_window_being_created_exactly_one_wins(client):
    pid = open_point(client)
    created = book(client, pid, "mover", tomorrow_at(9), tomorrow_at(10))
    target_start, target_end = tomorrow_at(11), tomorrow_at(12)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, dict]] = []

    def do_reschedule():
        barrier.wait()
        r = reschedule(
            client, pid, created["id"],
            reschedule_body(created, target_start, target_end, "into"),
        )
        outcomes.append(("move", r.status_code, r.json()))

    def do_create():
        barrier.wait()
        r = reserve(client, pid, "grabber", target_start, target_end)
        outcomes.append(("create", r.status_code, r.json()))

    threads = [threading.Thread(target=do_reschedule), threading.Thread(target=do_create)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted((name, status) for name, status, _ in outcomes)
    win_statuses = sorted(status for _, status in statuses)
    # The winner is either the reschedule (200) or the fresh creation (201).
    assert win_statuses in ([200, 409], [201, 409])
    loser = next(body for _, status, body in outcomes if status == 409)
    assert loser["error"]["code"] in {"reservation_overlap", "outside_open_hours"}
