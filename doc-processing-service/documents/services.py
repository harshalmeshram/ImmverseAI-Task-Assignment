from __future__ import annotations

import logging
import uuid
from functools import partial
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from redis.exceptions import RedisError

from .models import Document, Job
from .queue import JobQueue, get_queue
from .validators import validate_upload

logger = logging.getLogger(__name__)


def create_document(upload: UploadedFile | None) -> tuple[Document, Job]:
    """Validate and store an upload, create its Document + Job, and enqueue the job.

    The job is pushed to Redis only after the DB transaction commits, so a worker can never
    receive a job id that does not exist yet.
    """
    file_type = validate_upload(upload)
    document_id = uuid.uuid4()
    file_path = _save_file(document_id, file_type.extension, upload)

    try:
        with transaction.atomic():
            document = Document.objects.create(
                id=document_id,
                filename=Path(upload.name or "upload").name[:255],
                content_type=file_type.mime,
                size=upload.size,
                file_path=file_path,
            )
            job = Job.objects.create(document=document, max_attempts=settings.JOB_MAX_ATTEMPTS)
            transaction.on_commit(partial(enqueue_job, job.id))
    except Exception:
        Path(file_path).unlink(missing_ok=True)
        raise

    logger.info("document %s accepted, job %s queued", document.id, job.id)
    return document, job


def enqueue_job(job_id: uuid.UUID | str, delay: float = 0, queue: JobQueue | None = None) -> None:
    """Push a job id to Redis. A Redis outage must not fail the request: the job is already
    persisted as `queued`, and the workers' recovery sweep re-enqueues it later."""
    try:
        (queue or get_queue()).enqueue(str(job_id), delay=delay)
    except RedisError:
        logger.exception("could not enqueue job %s; the recovery sweep will re-enqueue it", job_id)


def _save_file(document_id: uuid.UUID, extension: str, upload: UploadedFile) -> str:
    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{document_id}{extension}"
    with path.open("wb") as out:
        for chunk in upload.chunks():
            out.write(chunk)
    return str(path)
