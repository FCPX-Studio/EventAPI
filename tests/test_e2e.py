"""
End-to-end tests over HTTP against the running production image.
Skipped unless E2E_BASE_URL is set (tests/run.sh sets it).
"""
import os

import httpx
import pytest

from tests.conftest import SEND_EVENT_PATH, encode, make_event, sign, stream_entries

BASE_URL = os.environ.get("E2E_BASE_URL")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="E2E_BASE_URL not set")


@pytest.fixture(scope="module")
def http():
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        yield c


def test_e2e_signed_event_reaches_redis(http, redis_client):
    body = make_event(event_type="customer.created")
    raw = encode(body)
    r = http.post(SEND_EVENT_PATH, content=raw, headers={"Content-Type": "application/json", "Paddle-Signature": sign(raw)})
    assert r.status_code == 200, r.text
    entries = stream_entries(redis_client)
    assert [e["data"]["paddle_event_id"] for e in entries] == [body["event_id"]]


def test_e2e_unsigned_event_is_rejected(http, redis_client):
    raw = encode(make_event())
    r = http.post(SEND_EVENT_PATH, content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 401
    assert stream_entries(redis_client) == []


def test_e2e_forged_signature_is_rejected(http, redis_client):
    raw = encode(make_event())
    r = http.post(
        SEND_EVENT_PATH,
        content=raw,
        headers={"Content-Type": "application/json", "Paddle-Signature": sign(raw, secret="attacker")},
    )
    assert r.status_code == 401
    assert stream_entries(redis_client) == []


def test_e2e_openapi(http):
    r = http.get("/events/v1/openapi.json")
    assert r.status_code == 200
    assert "/events/v1/paddle/send_event" in r.json()["paths"]
