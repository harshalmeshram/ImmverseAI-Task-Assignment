import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Response, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import services
from .db import get_session
from .exceptions import InvalidDocument, NotFound
from .models import Document, Job
from .queue import get_queue
from .schemas import DocumentAccepted, DocumentOut, ErrorOut, HealthOut, JobOut, ResultOut

logger = logging.getLogger(__name__)

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.post(
    "/documents",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentAccepted,
    responses={400: {"model": ErrorOut}, 413: {"model": ErrorOut}, 415: {"model": ErrorOut}},
    tags=["documents"],
)
def upload_document(
    session: SessionDep,
    response: Response,
    file: Annotated[UploadFile | None, File(description="PDF, PNG, JPEG or TIFF")] = None,
) -> DocumentAccepted:
    """Accept a document and queue it for asynchronous processing. Returns immediately."""
    if file is None:
        raise InvalidDocument("No file uploaded. Send the document as multipart/form-data in the 'file' field.")
    document, job = services.create_document(session, file.filename, file.file)
    response.headers["Location"] = f"/documents/{document.id}"
    return DocumentAccepted(document_id=document.id, job_id=job.id, status=job.status)


@router.get("/documents/{document_id}", response_model=DocumentOut, responses={404: {"model": ErrorOut}}, tags=["documents"])
def get_document(document_id: uuid.UUID, session: SessionDep) -> DocumentOut:
    """Document metadata, its latest job, and the extracted text once processing has completed."""
    document = session.get(Document, document_id)
    if document is None:
        raise NotFound("Document not found.")
    latest_job = session.scalars(
        select(Job).where(Job.document_id == document_id).order_by(Job.created_at.desc()).limit(1)
    ).first()
    return DocumentOut(
        id=document.id,
        filename=document.filename,
        content_type=document.content_type,
        size=document.size,
        status=document.status,
        created_at=document.created_at,
        updated_at=document.updated_at,
        latest_job=JobOut.model_validate(latest_job) if latest_job else None,
        result=ResultOut.model_validate(document.result) if document.result else None,
    )


@router.get("/jobs/{job_id}", response_model=JobOut, responses={404: {"model": ErrorOut}}, tags=["jobs"])
def get_job(job_id: uuid.UUID, session: SessionDep) -> Job:
    """Job status, attempts, timestamps and last error."""
    job = session.get(Job, job_id)
    if job is None:
        raise NotFound("Job not found.")
    return job


@router.get("/health", response_model=HealthOut, responses={503: {"model": HealthOut}}, tags=["ops"])
def health(session: SessionDep):
    checks = {}
    try:
        session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        logger.exception("health check: database unavailable")
        checks["database"] = "unavailable"
    try:
        get_queue().ping()
        checks["redis"] = "ok"
    except Exception:
        logger.exception("health check: redis unavailable")
        checks["redis"] = "unavailable"

    healthy = all(value == "ok" for value in checks.values())
    body = {"status": "ok" if healthy else "degraded", **checks}
    return JSONResponse(body, status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE)
