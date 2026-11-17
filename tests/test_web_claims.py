"""
POST /events/v1/web/orders/{txn}/licenses: license claims from the website's confirmation page.

Runs against a real Redis (like the rest of the suite), with CLAIM_WAIT_SECONDS=1 (tests/conftest.py).
Events are written to `licenses:issued` the way AuthAPI publishes them.
"""
import asyncio
import hashlib
import logging
import random
import string
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.main import app, api_prefix
from config import parse_origins
from app.website import ClaimLimits, ClaimWaiters, claim_matches
from events import IssuedItem, IssuedKey, IssuedLicenseTerms, IssuedProduct, LicenseIssued
from events.streams import LICENSES_ISSUED, encode

# Shared test vector (also used by the website's tests): the browser hashes the secret's raw bytes
VECTOR_SECRET = "00" * 32
VECTOR_HASH = "66687aadf862bd776c8fc18b8e9f8e20089714856ee233b3902a591d0d5f2925"

SECRET = "a1" * 32
CLAIM_HASH = hashlib.sha256(bytes.fromhex(SECRET)).hexdigest()
KEYS = ["AAAA-BBBB-CCCC-0001", "AAAA-BBBB-CCCC-0002"]


def txn_id() -> str:
    return "txn_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=26))


def claim_path(txn: str) -> str:
    return f"{api_prefix}/web/orders/{txn}/licenses"


def issued(txn: str, claim_hash: str | None = CLAIM_HASH, keys: list[str] = KEYS) -> LicenseIssued:
    product = IssuedProduct(bundle_id="com.fcpxstudio.trim", name="Trim Assist", type="plugin", version="1.2.0")
    terms = IssuedLicenseTerms(
        paddle_price_id="pri_01gsz8x8sawmvhz1pv30nge1ke", name="2 seats", description=None,
        activation_seats=2, life_duration_days=None, is_trial=False,
    )
    item = IssuedItem(
        paddle_item_id="txnitm_01hv8y1z2x3w4v5u6t7s8r9q0p", quantity=len(keys), product=product, license=terms,
        keys=[IssuedKey(license_key=k, item_index=i, expires_at=None) for i, k in enumerate(keys, 1)],
    )
    return LicenseIssued(order_id=1, paddle_order_id=txn, user_license_ids=list(range(1, len(keys) + 1)),
                         items=[item], claim_hash=claim_hash)


def publish(redis_client, event: LicenseIssued) -> None:
    redis_client.xadd(LICENSES_ISSUED, encode(LICENSES_ISSUED, event))


@pytest.fixture(autouse=True)
def clean_claims(redis_client):
    redis_client.delete(LICENSES_ISSUED)
    for key in redis_client.scan_iter("claims:rate:*"):
        redis_client.delete(key)
    yield


@pytest.fixture
def limits(client):
    """Swap the app's limits for the test, restored after"""
    original = app.state.claim_limits

    def use(**overrides) -> ClaimLimits:
        settings = dict(max_waits_per_ip=3, max_requests_per_minute=100_000, max_waits=500, wait_seconds=1)
        app.state.claim_limits = ClaimLimits(**{**settings, **overrides})
        return app.state.claim_limits

    yield use
    app.state.claim_limits = original


def claim(client, txn: str, secret: str = SECRET):
    return client.post(claim_path(txn), json={"claim_secret": secret})


# --------------------------------------------------------------------------- the secret


def test_shared_test_vector():
    """32 zero bytes, sent as 64 hex: the hash of the bytes, not of the hex text"""
    assert hashlib.sha256(bytes.fromhex(VECTOR_SECRET)).hexdigest() == VECTOR_HASH
    assert claim_matches(VECTOR_SECRET, issued(txn_id(), claim_hash=VECTOR_HASH))
    assert not claim_matches(VECTOR_SECRET, issued(txn_id(), claim_hash=hashlib.sha256(VECTOR_SECRET.encode()).hexdigest()))


def test_uppercase_secret_is_the_same_secret():
    assert claim_matches(SECRET.upper(), issued(txn_id()))


# --------------------------------------------------------------------------- answers


def test_right_secret_gets_the_keys(client, redis_client):
    txn = txn_id()
    publish(redis_client, issued(txn))
    started = time.monotonic()
    r = claim(client, txn)
    assert r.status_code == 200, r.text
    assert time.monotonic() - started < 0.5  # already in the stream: answered at once
    body = r.json()
    assert body["status"] == "licenses"
    assert [k["license_key"] for k in body["items"][0]["keys"]] == KEYS
    assert r.headers["cache-control"] == "no-store"


def test_wrong_secret_is_forbidden_and_gets_no_key(client, redis_client):
    txn = txn_id()
    publish(redis_client, issued(txn))
    r = claim(client, txn, secret="b2" * 32)
    assert r.json() == {"status": "forbidden", "items": None}
    assert not any(key in r.text for key in KEYS)
    assert r.headers["cache-control"] == "no-store"


def test_order_without_claim_hash_is_forbidden(client, redis_client):
    txn = txn_id()
    publish(redis_client, issued(txn, claim_hash=None))
    assert claim(client, txn).json()["status"] == "forbidden"


def test_no_event_is_pending_after_the_wait(client):
    started = time.monotonic()
    r = claim(client, txn_id())
    assert r.json() == {"status": "pending", "items": None}
    assert 0.9 <= time.monotonic() - started < 3  # CLAIM_WAIT_SECONDS=1
    assert r.headers["cache-control"] == "no-store"


def test_event_published_while_waiting(client, redis_client):
    txn = txn_id()
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(claim, client, txn)
        time.sleep(0.3)
        publish(redis_client, issued(txn))
        r = waiting.result()
    assert r.json()["status"] == "licenses"


def test_one_event_resolves_every_waiter_of_the_order(client, redis_client):
    txn = txn_id()
    with ThreadPoolExecutor(3) as pool:
        waiting = [pool.submit(claim, client, txn) for _ in range(3)]
        time.sleep(0.3)
        publish(redis_client, issued(txn))
        answers = [w.result().json()["status"] for w in waiting]
    assert answers == ["licenses"] * 3


def test_latest_event_of_the_order_wins(client, redis_client):
    """A replay after a revocation announces fewer keys: that's the current state"""
    txn = txn_id()
    publish(redis_client, issued(txn))
    publish(redis_client, issued(txn, keys=KEYS[:1]))
    keys = claim(client, txn).json()["items"][0]["keys"]
    assert [k["license_key"] for k in keys] == KEYS[:1]


def test_other_orders_events_dont_answer(client, redis_client):
    publish(redis_client, issued(txn_id()))
    assert claim(client, txn_id()).json()["status"] == "pending"


def test_waiters_and_slots_released(client, redis_client):
    txn = txn_id()
    claim(client, txn)  # pending
    publish(redis_client, issued(txn))
    claim(client, txn)  # licenses
    assert app.state.claims.waiting == 0
    assert app.state.claim_limits.waiting == 0


# --------------------------------------------------------------------------- input


@pytest.mark.parametrize("txn", ["txn_short", "pri_" + "a" * 26, "txn_" + "A" * 26, "txn_" + "a" * 27])
def test_bad_order_id_is_422(client, txn):
    assert claim(client, txn).status_code == 422


@pytest.mark.parametrize("secret", ["", "a1" * 31, "a1" * 33, "zz" * 32, None, 123])
def test_bad_secret_is_422(client, secret):
    assert client.post(claim_path(txn_id()), json={"claim_secret": secret}).status_code == 422


def test_secret_never_in_the_url(client):
    assert client.get(claim_path(txn_id()), params={"claim_secret": SECRET}).status_code == 405


# --------------------------------------------------------------------------- limits


def test_fourth_concurrent_wait_from_one_ip_is_429(client, limits):
    limits(max_waits_per_ip=3)
    with ThreadPoolExecutor(3) as pool:
        waiting = [pool.submit(claim, client, txn_id()) for _ in range(3)]
        time.sleep(0.3)
        r = claim(client, txn_id())
        assert [w.result().json()["status"] for w in waiting] == ["pending"] * 3
    assert r.status_code == 429
    assert r.headers["retry-after"] == "1"
    assert claim(client, txn_id()).status_code == 200  # the slots were released


def test_requests_per_minute_per_ip(client, redis_client, limits):
    limits(max_requests_per_minute=2)
    txn = txn_id()
    publish(redis_client, issued(txn))
    assert [claim(client, txn).status_code for _ in range(3)] == [200, 200, 429]
    r = claim(client, txn)
    assert 1 <= int(r.headers["retry-after"]) <= 60


def test_global_cap_is_503(client, limits):
    limits(max_waits=1, max_waits_per_ip=10)
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(claim, client, txn_id())
        time.sleep(0.3)
        r = claim(client, txn_id())
        assert waiting.result().json()["status"] == "pending"
    assert r.status_code == 503
    assert "retry-after" in r.headers


def test_refusals_logged_once_per_ip_per_minute(client, redis_client, limits, caplog):
    limits(max_requests_per_minute=1)
    txn = txn_id()
    publish(redis_client, issued(txn))
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            claim(client, txn)
    assert len([r for r in caplog.records if "Claim refused" in r.getMessage()]) == 1


# --------------------------------------------------------------------------- logs, CORS


def test_secret_hash_and_keys_never_logged(client, redis_client, caplog):
    txn = txn_id()
    publish(redis_client, issued(txn))
    with caplog.at_level(logging.DEBUG):
        claim(client, txn)
        claim(client, txn, secret="b2" * 32)
    for secret in (SECRET, "b2" * 32, CLAIM_HASH, *KEYS):
        assert secret not in caplog.text
    assert f"Claim licenses | paddle_order_id={txn}" in caplog.text


def test_cors_allows_the_website_only(client):
    def preflight(origin: str):
        return client.options(claim_path(txn_id()), headers={
            "Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type",
        })

    for origin in ("https://website.test", "https://www.website.test"):  # both entries of WEBSITE_ORIGIN
        assert preflight(origin).headers.get("access-control-allow-origin") == origin
    assert "access-control-allow-origin" not in preflight("https://evil.test").headers


@pytest.mark.parametrize("value, expected", [
    ("https://fcpxstudio.shop", ["https://fcpxstudio.shop"]),
    ("https://www.fcpxstudio.shop, https://fcpxstudio.shop/", ["https://www.fcpxstudio.shop", "https://fcpxstudio.shop"]),
    ("HTTPS://FcpxStudio.shop/,https://fcpxstudio.shop", ["https://fcpxstudio.shop"]),  # normalized, deduplicated
    ("http://localhost:3000,", ["http://localhost:3000"]),
])
def test_website_origins_parsed(value, expected):
    assert parse_origins(value) == expected


@pytest.mark.parametrize("value", ["", " , ", "fcpxstudio.shop", "https://fcpxstudio.shop/en", "ftp://fcpxstudio.shop", "*"])
def test_invalid_website_origins_are_refused(value):
    with pytest.raises(ValueError):
        parse_origins(value)


# --------------------------------------------------------------------------- the reader race


def test_event_between_registering_and_the_scan_is_not_missed():
    """
    The waiter is registered before the stream is scanned: an event the reader delivers during the scan
    resolves it (the scan itself found nothing)
    """
    event = issued(txn_id())

    class RacingWaiters(ClaimWaiters):
        async def recent(self, paddle_order_id):
            self._resolve(event)  # the reader delivers it mid-scan
            return None

    async def run():
        waiters = RacingWaiters(client=object())  # type: ignore[arg-type]  # Redis is never called
        return await waiters.wait(event.paddle_order_id, timeout=1), waiters.waiting

    found, left = asyncio.run(run())
    assert found == event and left == 0
