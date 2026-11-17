from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.schemas.web import ClaimRequest, ClaimResponse
from app.schemas.paddle import PaddleOrderId
from app.website import ClaimLimits, ClaimWaiters, claim_matches

from config import settings
from logger import logger

from app.schemas.exceptions import LimitExceededError
from redis.exceptions import RedisError

import time


# Called by the website from the buyer's browser (CORS: settings.WEBSITE_ORIGIN only)
router = APIRouter(
    prefix='/web',
    tags=["web"]
)

@router.post(
    '/orders/{paddle_order_id}/licenses',
    response_model=ClaimResponse,
    responses={
        422: {"description": "Malformed order ID or claim secret"},
        429: {"description": "Too many requests, or too many waiting requests, from this IP (Retry-After)"},
        503: {"description": "Too many waiting requests, or Redis unavailable (Retry-After)"},
    },
)
async def claim_licenses(paddle_order_id: PaddleOrderId, body: ClaimRequest, request: Request, response: Response):
    """
    The license keys of an order, for the browser that holds its claim secret (long-poll).

    Waits up to CLAIM_WAIT_SECONDS for the order's LicenseIssued (answered at once if it's already in the
    stream), then answers once:
    - `licenses` + items: sha256(secret) is the order's claim_hash
    - `forbidden`: the secret doesn't match, or the order has no claim hash
    - `pending`: no event in time (the page shows the email message)

    Nothing is answered before the event arrives, so the response time doesn't reveal whether the order
    exists. Never log the secret, the hash or the items.
    """
    claims: ClaimWaiters = request.app.state.claims
    limits: ClaimLimits = request.app.state.claim_limits
    client_ip = request.client.host if request.client else "unknown"
    started = time.monotonic()


    # Called by the website from the buyer's browser (CORS: settings.WEBSITE_ORIGIN only)
    no_store = {"Cache-Control": "no-store"}

    try:
        async with limits.slot(client_ip):
            event = await claims.wait(paddle_order_id, settings.CLAIM_WAIT_SECONDS)
    except LimitExceededError as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"detail": e.reason},
            headers={**no_store, "Retry-After": str(e.retry_after)},
        )
    except RedisError as e:
        logger.warning(f"Claim failed: Redis error ({type(e).__name__}) | paddle_order_id={paddle_order_id}")
        return JSONResponse(
            status_code=503, content={"detail": "unavailable"}, headers={**no_store, "Retry-After": "5"}
        )

    if event is None:
        answer = ClaimResponse(status="pending")
    elif claim_matches(body.claim_secret, event):
        answer = ClaimResponse(status="licenses", items=event.items)
    else:
        answer = ClaimResponse(status="forbidden")

    response.headers.update(no_store)
    logger.info(
        f"Claim {answer.status} | paddle_order_id={paddle_order_id} waited_ms={int((time.monotonic() - started) * 1000)}"
    )
    return answer
