from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import BinaryIO

from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from .config import settings
from .models import Document, Job
from .queue import JobQueue, get_queue
from .validators import file_size, validate_upload

logger = logging.getLogger(__name__)


def create_document(session: Session, filename: str | None, file: BinaryIO) -> tuple[Document, Job]:
    """Validate and store an upload, create its Document + Job, and enqueue the job.

    The job is pushed to Redis only after the DB transaction commits, so a worker can never
    receive a job id that does not exist yet.
    """
    size = file_size(file)
    file_type = validate_upload(file, size)
    document_id = uuid.uuid4()
    file_path = _save_file(document_id, file_type.extension, file)

    try:
        document = Document(
            id=document_id,
            filename=Path(filename or "upload").name[:255],
            content_type=file_type.mime,
            size=size,
            file_path=file_path,
        )
        job = Job(document=document, max_attempts=settings.job_max_attempts)
        session.add_all([document, job])
        session.commit()
    except Exception:
        session.rollback()
        Path(file_path).unlink(missing_ok=True)
        raise

    enqueue_job(job.id)
    logger.info("document %s accepted, job %s queued", document.id, job.id)
    return document, job


def enqueue_job(job_id: uuid.UUID | str, delay: float = 0, queue: JobQueue | None = None) -> None:
    """Push a job id to Redis. A Redis outage must not fail the request: the job is already
    persisted as `queued`, and the workers' recovery sweep re-enqueues it later."""
    try:
        (queue or get_queue()).enqueue(str(job_id), delay=delay)
    except RedisError:
        logger.exception("could not enqueue job %s; the recovery sweep will re-enqueue it", job_id)


def _save_file(document_id: uuid.UUID, extension: str, file: BinaryIO) -> str:
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{document_id}{extension}"
    file.seek(0)
    with path.open("wb") as out:
        while chunk := file.read(1024 * 1024):
            out.write(chunk)
    return str(path.resolve())
