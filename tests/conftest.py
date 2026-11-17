"""
Shared fixtures.

Settings are read from the environment at import time (config.settings, events.RedisSettings),
so the test environment must be set before `app` is imported. tests/run.sh sets it when the
suite runs in Docker; the defaults below let it also run locally against a Redis on localhost.
"""
import hashlib
import hmac
import json
import os
import time
import uuid

os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("REDIS_PASSWORD", "")
os.environ.setdefault("PADDLE_WEBHOOK_SECRET", "pdl_ntfset_test_secret")
os.environ.setdefault("WEBSITE_ORIGIN", "https://website.test, https://www.website.test/")
# Short waits and loose rate limits for the suite: tests/test_web_claims.py sets tight ones where it tests them
os.environ.setdefault("CLAIM_WAIT_SECONDS", "1")
os.environ.setdefault("CLAIM_MAX_REQUESTS_PER_MINUTE", "100000")

import pytest
import redis
from fastapi.testclient import TestClient

from app.main import app, api_prefix
from events.streams import PADDLE_EVENTS

SECRET = os.environ["PADDLE_WEBHOOK_SECRET"]
SEND_EVENT_PATH = f"{api_prefix}/paddle/send_event"


def make_event(**overrides) -> dict:
    """A Paddle webhook body shaped like the real thing (transaction.completed)"""
    body = {
        "event_id": f"evt_{uuid.uuid4().hex[:26]}",
        "event_type": "transaction.completed",
        "occurred_at": "2026-11-16T10:00:00.000000Z",
        "notification_id": f"ntf_{uuid.uuid4().hex[:26]}",
        "data": {
            "id": "txn_01hv8wptq8987qeep44cyrewp9",
            "status": "completed",
            "customer_id": "ctm_01hv6y1jedq4p1n0yqn5ba3ky4",
            "items": [{"price": {"id": "pri_01gsz8x8sawmvhz1pv30nge1ke"}, "quantity": 1}],
        },
    }
    body.update(overrides)
    return body


def sign(raw: bytes, secret: str = SECRET, ts: int | None = None) -> str:
    """Build a Paddle-Signature header the way Paddle does: ts=<unix>;h1=HMAC_SHA256(secret, "<ts>:<body>")"""
    ts = int(time.time()) if ts is None else ts
    h1 = hmac.new(secret.encode(), f"{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


def encode(body: dict) -> bytes:
    return json.dumps(body).encode()


@pytest.fixture(scope="session")
def redis_client() -> redis.Redis:
    client = redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        password=os.environ["REDIS_PASSWORD"] or None,
        decode_responses=True,
    )
    client.ping()
    return client


@pytest.fixture(autouse=True)
def clean_stream(redis_client):
    redis_client.delete(PADDLE_EVENTS)
    yield


@pytest.fixture(scope="module")
def client():
    # Context manager runs the lifespan (creates app.state.publisher / app.state.paddle)
    with TestClient(app) as c:
        yield c


def stream_entries(redis_client) -> list[dict]:
    """Decoded entries of the paddle:events stream, oldest first"""
    out = []
    for entry_id, fields in redis_client.xrange(PADDLE_EVENTS):
        out.append({"id": entry_id, "type": fields["type"], "data": json.loads(fields["data"])})
    return out
