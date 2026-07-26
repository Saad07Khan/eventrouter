from pydantic import BaseModel


class SourceCreate(BaseModel):
    name: str


class SourceCreated(BaseModel):
    id: str
    name: str
    write_key: str  # returned ONCE at creation; we can't show it again (only the hash is stored)


class TrackIn(BaseModel):
    type: str
    payload: dict


class TrackAccepted(BaseModel):
    id: str
    status: str = "accepted"
