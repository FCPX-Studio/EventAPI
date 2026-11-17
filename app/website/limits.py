"""
Abuse limits of the claim route.

A claim request can make the server wait up to CLAIM_WAIT_SECONDS: that wait is the only thing a client can
make us spend, so the limits cap how many requests wait at once, per client IP and in total, plus a request
rate per IP. Checked before the request waits, so a refused request costs almost nothing.

The client IP is request.client.host: uvicorn runs with --proxy-headers, and Caddy replaces any
X-Forwarded-For sent by the client (its default while `trusted_proxies` isn't set in the Caddyfile).
"""
from app.schemas.exceptions import LimitExceededError

from events.client import get_redis

from logger import logger

from redis.asyncio import Redis
from redis.exceptions import RedisError

import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ClaimLimits:
    """
    - Concurrent waits per IP (in-process): the main defense, one client can't fill the server with waits.
    - Requests per IP per minute (Redis, holds across restarts and replicas). Fails open if Redis is down.
    - Concurrent waits in total (in-process): the backstop against many IPs at once.
    """

    def __init__(
        self,
        max_waits_per_ip: int,
        max_requests_per_minute: int,
        max_waits: int,
        wait_seconds: float,
        client: Redis | None = None,
    ) -> None:
        """
        Args:
            max_waits_per_ip (int): Requests of one IP waiting at once (429 beyond)
            max_requests_per_minute (int): Requests of one IP per calendar minute (429 beyond)
            max_waits (int): Requests waiting at once, all IPs (503 beyond)
            wait_seconds (float): How long a request can wait, the Retry-After of a concurrency refusal
            client (Redis | None): Override for tests, defaults to the shared client
        """
        self._max_waits_per_ip = max_waits_per_ip
        self._max_requests_per_minute = max_requests_per_minute
        self._max_waits = max_waits
        self._wait_seconds = wait_seconds
        self._client = client or get_redis()
        self._waits_per_ip: dict[str, int] = defaultdict(int)
        self._waits = 0
        # Refusals are logged at most once per IP per minute, so an attack can't flood the logs
        self._logged_minute = 0
        self._logged_ips: set[str] = set()

    @property
    def waiting(self) -> int:
        return self._waits

    @asynccontextmanager
    async def slot(self, ip: str) -> AsyncIterator[None]:
        """
        Hold a waiting slot for one request, released on exit (answered, timed out or failed).

        Raises:
            LimitExceededError: 429 (rate or per-IP concurrency) or 503 (global concurrency)
        """
        try:
            await self._check_rate(ip)
            self._check_concurrency(ip)
        except LimitExceededError as e:
            self._log_refusal(ip, e)
            raise

        self._waits += 1
        self._waits_per_ip[ip] += 1
        try:
            yield
        finally:
            self._waits -= 1
            self._waits_per_ip[ip] -= 1
            if self._waits_per_ip[ip] <= 0:
                del self._waits_per_ip[ip]

    async def _check_rate(self, ip: str) -> None:
        """Fixed window per calendar minute: INCR claims:rate:<ip>:<minute>, expires after 2 minutes"""
        now = time.time()
        minute = int(now // 60)
        key = f"claims:rate:{ip}:{minute}"
        try:
            async with self._client.pipeline(transaction=False) as pipe:
                pipe.incr(key)
                pipe.expire(key, 120)
                count, _ = await pipe.execute()
        except RedisError as e:
            # Fail open: the concurrency caps still apply, and the route can't answer without Redis anyway
            logger.warning(f"Claim rate limit skipped: Redis error ({type(e).__name__})")
            return
        if count > self._max_requests_per_minute:
            retry_after = max(1, int((minute + 1) * 60 - now))
            raise LimitExceededError(429, retry_after, "too many requests")

    def _check_concurrency(self, ip: str) -> None:
        if self._waits_per_ip.get(ip, 0) >= self._max_waits_per_ip:
            raise LimitExceededError(429, int(self._wait_seconds), "too many waiting requests from this IP")
        if self._waits >= self._max_waits:
            raise LimitExceededError(503, 5, "too many waiting requests")

    def _log_refusal(self, ip: str, e: LimitExceededError) -> None:
        minute = int(time.time() // 60)
        if minute != self._logged_minute:
            self._logged_minute, self._logged_ips = minute, set()
        if ip in self._logged_ips:
            return
        self._logged_ips.add(ip)
        logger.warning(f"Claim refused ({e.status_code}, {e.reason}) | client_ip={ip} waiting={self._waits}")
