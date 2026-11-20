import uuid
from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict

from .models import Status


def _as_utc(value: datetime) -> datetime:
    # All timestamps are written in UTC. PostgreSQL returns them timezone-aware; SQLite drops the
    # timezone, so treat naive values as UTC. The API then always emits ISO 8601 with "Z".
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]


class DocumentAccepted(BaseModel):
    document_id: uuid.UUID
    job_id: uuid.UUID
    status: Status


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    status: Status
    attempts: int
    max_attempts: int
    error: str | None
    created_at: UTCDateTime
    started_at: UTCDateTime | None
    completed_at: UTCDateTime | None
    updated_at: UTCDateTime


class ResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    text: str
    created_at: UTCDateTime


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    size: int
    status: Status
    created_at: UTCDateTime
    updated_at: UTCDateTime
    latest_job: JobOut | None
    result: ResultOut | None


class HealthOut(BaseModel):
    status: str
    database: str
    redis: str


class ErrorOut(BaseModel):
    detail: str
