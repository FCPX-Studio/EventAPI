"""
In-process tests of POST /events/v1/paddle/send_event against a real Redis.
"""
import time

import pytest
from redis.asyncio import Redis

from events import EventPublisher
from tests.conftest import SEND_EVENT_PATH, encode, make_event, sign, stream_entries


def post(client, raw: bytes, signature: str | None, header: str = "Paddle-Signature"):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers[header] = signature
    return client.post(SEND_EVENT_PATH, content=raw, headers=headers)


# ---------- Happy path ----------

def test_valid_event_is_published(client, redis_client):
    body = make_event()
    raw = encode(body)

    r = post(client, raw, sign(raw))

    assert r.status_code == 200, r.text
    entries = stream_entries(redis_client)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["type"] == "paddle.event"
    assert entry["data"]["paddle_event_id"] == body["event_id"]
    assert entry["data"]["event_type"] == body["event_type"]
    assert entry["data"]["payload"] == body["data"]
    # The envelope gets its own id/timestamp, distinct from Paddle's
    assert entry["data"]["event_id"] != body["event_id"]


def test_signature_header_is_case_insensitive(client, redis_client):
    raw = encode(make_event())
    r = post(client, raw, sign(raw), header="paddle-signature")
    assert r.status_code == 200, r.text
    assert len(stream_entries(redis_client)) == 1


def test_key_rotation_multiple_h1_one_valid(client, redis_client):
    raw = encode(make_event())
    good = sign(raw)
    ts, h1 = good.split(";")
    r = post(client, raw, f"{ts};h1={'0' * 64};{h1}")
    assert r.status_code == 200, r.text
    assert len(stream_entries(redis_client)) == 1


def test_paddle_retry_same_event_is_published_twice(client, redis_client):
    """No dedup at the API: Paddle retries produce duplicate stream entries, consumers must be idempotent"""
    raw = encode(make_event())
    assert post(client, raw, sign(raw)).status_code == 200
    assert post(client, raw, sign(raw)).status_code == 200
    entries = stream_entries(redis_client)
    assert len(entries) == 2
    assert entries[0]["data"]["paddle_event_id"] == entries[1]["data"]["paddle_event_id"]


def test_unknown_extra_fields_are_accepted(client, redis_client):
    raw = encode(make_event(extra_field="whatever"))
    assert post(client, raw, sign(raw)).status_code == 200


# ---------- Authentication ----------

def test_missing_signature_is_401(client, redis_client):
    raw = encode(make_event())
    r = post(client, raw, None)
    assert r.status_code == 401
    assert stream_entries(redis_client) == []


def test_wrong_secret_is_401(client, redis_client):
    raw = encode(make_event())
    r = post(client, raw, sign(raw, secret="not-the-secret"))
    assert r.status_code == 401
    assert stream_entries(redis_client) == []


def test_tampered_body_is_401(client, redis_client):
    raw = encode(make_event())
    signature = sign(raw)
    tampered = encode({**make_event(), "event_type": "subscription.canceled"})
    r = post(client, tampered, signature)
    assert r.status_code == 401
    assert stream_entries(redis_client) == []


def test_replayed_old_timestamp_is_401(client, redis_client):
    raw = encode(make_event())
    r = post(client, raw, sign(raw, ts=int(time.time()) - 60))
    assert r.status_code == 401
    assert stream_entries(redis_client) == []


def test_future_timestamp_is_401(client, redis_client):
    raw = encode(make_event())
    r = post(client, raw, sign(raw, ts=int(time.time()) + 60))
    assert r.status_code == 401


def test_header_without_key_value_pairs_is_401(client):
    raw = encode(make_event())
    assert post(client, raw, "garbage").status_code == 401


@pytest.mark.xfail(
    strict=True,
    reason="BUG: paddle SDK raises ValueError on a malformed Paddle-Signature header, "
    "uncaught in Paddle.validate_webhook_signature -> 500 instead of 401",
)
@pytest.mark.parametrize(
    "header",
    [
        "ts=notanumber;h1=abc",          # int() fails in the SDK
        "ts=1;h1=abc;foo=bar",           # SDK raises "Unrecognized Paddle-Signature key"
        "ts=1=2;h1=abc",                 # split('=') unpack error
    ],
)
def test_malformed_signature_header_is_401(client, header):
    raw = encode(make_event())
    assert post(client, raw, header).status_code == 401


# ---------- Body validation ----------

@pytest.mark.parametrize("missing", ["event_id", "event_type", "occurred_at", "notification_id", "data"])
def test_missing_required_field_is_422(client, missing):
    body = make_event()
    del body[missing]
    raw = encode(body)
    assert post(client, raw, sign(raw)).status_code == 422


def test_naive_datetime_is_422(client):
    raw = encode(make_event(occurred_at="2026-11-16T10:00:00"))
    assert post(client, raw, sign(raw)).status_code == 422


def test_data_not_an_object_is_422(client):
    raw = encode(make_event(data=["not", "a", "dict"]))
    assert post(client, raw, sign(raw)).status_code == 422


def test_non_json_body_is_422(client):
    raw = b"not json"
    assert post(client, raw, sign(raw)).status_code == 422


def test_unsigned_invalid_body_returns_422_not_401(client):
    """
    Documents current behavior: body validation runs before the signature check,
    so an unauthenticated caller gets a 422 with the expected schema instead of a 401.
    """
    r = post(client, b"{}", None)
    assert r.status_code == 422


# ---------- Redis failure ----------

def test_redis_unreachable_is_502(client, redis_client):
    original = client.app.state.publisher
    client.app.state.publisher = EventPublisher(client=Redis(host="127.0.0.1", port=1, socket_connect_timeout=1))
    try:
        raw = encode(make_event())
        r = post(client, raw, sign(raw))
        assert r.status_code == 502
        assert r.json() == {"detail": "Failed to send event to Redis"}
    finally:
        client.app.state.publisher = original


# ---------- Routing / docs ----------

def test_get_on_send_event_is_405(client):
    assert client.get(SEND_EVENT_PATH).status_code == 405


def test_route_without_prefix_is_404(client):
    raw = encode(make_event())
    assert post_raw(client, "/paddle/send_event", raw).status_code == 404


def post_raw(client, path, raw):
    return client.post(path, content=raw, headers={"Content-Type": "application/json", "Paddle-Signature": sign(raw)})


def test_openapi_and_docs_served_under_prefix(client):
    assert client.get("/events/v1/openapi.json").status_code == 200
    assert client.get("/events/v1/docs").status_code == 200
    assert client.get("/docs").status_code == 404
