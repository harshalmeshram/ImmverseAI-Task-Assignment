import io
import uuid

import fakeredis
import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import settings
from app.db import Base, configure_engine, new_session
from app.main import app
from app.models import Document, Job
from app.processing import DocumentProcessor
from app.queue import JobQueue, set_queue
from app.services import create_document
from app.worker import Worker


@pytest.fixture(autouse=True)
def queue(tmp_path, monkeypatch):
    """Every test gets its own SQLite database, in-memory Redis and upload directory."""
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    engine = configure_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    Base.metadata.create_all(engine)
    job_queue = JobQueue(fakeredis.FakeRedis(decode_responses=True), "test:jobs")
    set_queue(job_queue)
    yield job_queue
    set_queue(None)
    engine.dispose()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def make_pdf(text: str = "Hello World") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def make_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def new_job(text: str = "Hello World") -> Job:
    with new_session() as session:
        _, job = create_document(session, "doc.pdf", io.BytesIO(make_pdf(text)))
    return job


def get_job(job_id: uuid.UUID) -> Job:
    with new_session() as session:
        return session.get(Job, job_id)


def get_document(document_id: uuid.UUID) -> Document:
    with new_session() as session:
        return session.get(Document, document_id)


class StubProcessor(DocumentProcessor):
    """Plays back a list of outcomes (text or exception); the last one repeats."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or ["extracted text"]
        self.calls = 0

    def process(self, file_path, content_type):
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def make_worker(queue):
    def _make(processor, **overrides):
        options = {"poll_timeout": 1, "retry_backoff": 0, "stale_after": 300, "sweep_interval": 3600}
        options.update(overrides)
        return Worker(queue=queue, processor=processor, **options)

    return _make
