from pydantic import BaseModel, AwareDatetime


class RawPaddleEvent(BaseModel):
    event_id: str
    event_type: str
    occurred_at: AwareDatetime
    notification_id: str
    data: dict