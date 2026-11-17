"""
License claims from the website's confirmation page.

AuthAPI publishes one OrderLicensed per order on `orders:licensed` (kept 1 hour), keys included. The page
waits for its own order's event, and gets the keys only if it holds the claim secret whose SHA-256 is the
event's `claim_hash` (Order.claim_hash, set at checkout).

One background reader follows the stream for every waiting request (ClaimWaiters.run): a waiting request
is a Future in memory, it holds no Redis connection.
"""
from events import EventSubscriber, OrderLicensed
from events.client import get_redis
from events.streams import ORDERS_LICENSED, InvalidEvent, decode

from logger import logger

from redis.asyncio import Redis
from redis.exceptions import RedisError

import asyncio
import hashlib
import hmac
from collections import defaultdict


def claim_matches(claim_secret: str, event: OrderLicensed) -> bool:
    """
    True if the secret hashes to the event's claim_hash. Constant time. An order with no hash never matches.

    The browser hashes the secret's 32 raw bytes and sends them as 64 hex characters, so the hex is decoded
    back to bytes before hashing (hashing the hex text would give another hash).

    Args:
        claim_secret (str): 64 hex characters, validated by the route
        event (OrderLicensed): The order's event
    """
    actual = hashlib.sha256(bytes.fromhex(claim_secret)).hexdigest()
    return hmac.compare_digest(actual, event.claim_hash or "")


class ClaimWaiters:
    """
    Requests waiting for an order's OrderLicensed, resolved by one shared reader of `orders:licensed`.

    Every EventAPI instance runs its own reader: they all see every event (EventSubscriber, no group),
    so it stays correct with several replicas.
    """

    # Wait before following the stream again after a Redis error
    RETRY_SECONDS = 1.0

    def __init__(self, client: Redis | None = None, block_ms: int = 5000) -> None:
        """
        Args:
            client (Redis | None): Override for tests, defaults to the shared client
            block_ms (int): How long each read of the stream waits for new events
        """
        self._client = client or get_redis()
        self._block_ms = block_ms
        self._waiters: dict[str, set[asyncio.Future[OrderLicensed]]] = defaultdict(set)
        self._task: asyncio.Task | None = None

    # --- Lifecycle

    async def start(self) -> None:
        """Start following the stream, from its latest entry (older ones are found by `recent`)"""
        subscriber = EventSubscriber(ORDERS_LICENSED, client=self._client, block_ms=self._block_ms)
        cursor = await subscriber.cursor()
        self._task = asyncio.create_task(self._run(subscriber, cursor), name="claims-reader")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self, subscriber: EventSubscriber, cursor: str) -> None:
        """
        Resolve the waiters of each new event. A Redis error doesn't stop the reader: it follows the stream
        again from the last entry it read, so no event is skipped
        """
        while True:
            try:
                async for entry_id, event in subscriber.listen(cursor):
                    cursor = entry_id
                    self._resolve(event)  # type: ignore[arg-type]  # orders:licensed only carries OrderLicensed
            except asyncio.CancelledError:
                raise
            except RedisError as e:
                logger.warning(f"Claims reader: Redis error, retrying in {self.RETRY_SECONDS:.0f}s ({type(e).__name__})")
                await asyncio.sleep(self.RETRY_SECONDS)
            except Exception:
                logger.exception("Claims reader failed, restarting")
                await asyncio.sleep(self.RETRY_SECONDS)

    def _resolve(self, event: OrderLicensed) -> None:
        for future in self._waiters.pop(event.paddle_order_id, ()):
            if not future.done():
                future.set_result(event)

    # --- Waiting

    @property
    def waiting(self) -> int:
        """Number of requests waiting (for logs and tests)"""
        return sum(len(futures) for futures in self._waiters.values())

    async def recent(self, paddle_order_id: str) -> OrderLicensed | None:
        """
        The latest OrderLicensed of an order still in the stream (1 hour retention), None if none.
        The latest one wins: a replay after a revocation announces fewer keys than the first event
        """
        for _, fields in await self._client.xrevrange(ORDERS_LICENSED):
            # Cheap filter before decoding: every other order's entries are skipped as plain text
            if paddle_order_id not in fields.get("data", ""):
                continue
            try:
                event = decode(ORDERS_LICENSED, fields)
            except InvalidEvent:
                continue  # logged by the reader when it was published
            if isinstance(event, OrderLicensed) and event.paddle_order_id == paddle_order_id:
                return event
        return None

    async def wait(self, paddle_order_id: str, timeout: float) -> OrderLicensed | None:
        """
        The order's OrderLicensed: already in the stream (a late or reloaded page), or published within
        `timeout` seconds. None if it didn't come in time.

        The waiter is registered *before* the stream is scanned, so an event published in between is
        caught by one or the other, never missed.
        """
        future: asyncio.Future[OrderLicensed] = asyncio.get_running_loop().create_future()
        self._waiters[paddle_order_id].add(future)
        try:
            found = await self.recent(paddle_order_id)
            if found is not None:
                return found
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError:
            return None
        finally:
            self._unregister(paddle_order_id, future)

    def _unregister(self, paddle_order_id: str, future: asyncio.Future[OrderLicensed]) -> None:
        futures = self._waiters.get(paddle_order_id)
        if futures is None:
            return
        futures.discard(future)
        if not futures:
            del self._waiters[paddle_order_id]
