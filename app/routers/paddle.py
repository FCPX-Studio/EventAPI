from fastapi import APIRouter, Request, HTTPException

from app.schemas.events import PaddleEventHook
from app.paddle import Paddle

from events import EventPublisher, PaddleEvent
from events.streams import PADDLE_EVENTS


router = APIRouter(
    prefix='/paddle',
    tags=["paddle"]
)



@router.post('/send_event')
async def send_event(body: PaddleEventHook, request: Request) -> None:
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

    # Certify request comes from Paddle webhook
    if not await paddle.validate_webhook_signature(request):
        raise HTTPException(
            status_code=401,
            detail="Cannot validate webhook signature"
        )
    
    # Send event to Redis
    try:
        await publisher.publish(
            PADDLE_EVENTS, 
            PaddleEvent(
                paddle_event_id=body.event_id,
                event_type=body.event_type,
                payload=body.data
            )
        )
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Failed to send event to Redis"
        )
