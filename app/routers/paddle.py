from fastapi import APIRouter, Request, HTTPException

from app.schemas.events import RawPaddleEvent
from app.paddle import Paddle

from events import EventPublisher, PaddleEvent
from events.streams import PADDLE_EVENTS

from logger import logger


router = APIRouter(
    prefix='/paddle',
    tags=["paddle"]
)



@router.post('/send_event')
async def send_event(body: RawPaddleEvent, request: Request) -> None:
    """
    Publish a Paddle webhook event to redis.
    The request is authenticated using paddle secret key in request header

    Args:
        body (PaddleEvent): Generic paddle event
        request (Request): Default request argument

    Raises:
        HTTPException: 401 for bad body signature
        HTTPException: 502 if failed to send event to Redis
    """
    publisher: EventPublisher = request.app.state.publisher
    paddle: Paddle = request.app.state.paddle

    # Never log body.data: it holds customer details
    event_ctx = f"event_id={body.event_id} event_type={body.event_type} notification_id={body.notification_id}"
    client_ip = request.client.host if request.client else "unknown"

    # Certify request comes from Paddle webhook
    if not await paddle.validate_webhook_signature(request):
        logger.warning(f"Paddle webhook rejected: invalid signature | {event_ctx} client_ip={client_ip}")
        raise HTTPException(
            status_code=401,
            detail="Cannot validate webhook signature"
        )

    # Send event to Redis
    try:
        entry_id = await publisher.publish(
            PADDLE_EVENTS,
            PaddleEvent(
                paddle_event_id=body.event_id,
                event_type=body.event_type,
                payload=body.data
            )
        )
    except Exception:
        logger.exception(f"Paddle webhook failed: cannot publish to Redis | {event_ctx}")
        raise HTTPException(
            status_code=502,
            detail="Failed to send event to Redis"
        )

    logger.info(f"Paddle webhook published | {event_ctx} occurred_at={body.occurred_at.isoformat()} stream_entry={entry_id}")
