from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SourceCreate(BaseModel):
    name: str


class SourceCreated(BaseModel):
    id: str
    name: str
    write_key: str  # returned ONCE at creation; we can't show it again (only the hash is stored)


class DestinationCreate(BaseModel):
    source_id: str
    type: Literal["http", "slack", "warehouse"]
    config: dict = {}
    filter: str = "*"
    transform: dict = {}
    batch_size: int = 1
    batch_window_s: int = 0
    enabled: bool = True


class DestinationCreated(BaseModel):
    id: str
    type: str
    filter: str


class TrackIn(BaseModel):
    type: str
    payload: dict


class TrackAccepted(BaseModel):
    id: str
    status: str = "accepted"


class DeliveryOut(BaseModel):
    id: str
    destination_id: str
    destination_type: str
    status: str
    attempts: int
    last_error: str | None
    delivered_at: datetime | None
    next_attempt_at: datetime


class EventDetail(BaseModel):
    id: str
    type: str
    payload: dict
    received_at: datetime
    deliveries: list[DeliveryOut]


class ReplayResult(BaseModel):
    replayed: int


class DestinationStats(BaseModel):
    destination_id: str
    pending: int
    delivering: int
    delivered: int
    dead: int
    avg_attempts: float | None
