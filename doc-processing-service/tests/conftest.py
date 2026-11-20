import io

import fakeredis
import pymupdf
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from documents.processing import DocumentProcessor
from documents.queue import JobQueue, set_queue
from documents.worker import Worker


@pytest.fixture(autouse=True)
def queue(settings, tmp_path):
    """Every test gets an isolated in-memory Redis and upload directory."""
    settings.UPLOAD_DIR = str(tmp_path / "uploads")
    job_queue = JobQueue(fakeredis.FakeRedis(decode_responses=True), "test:jobs")
    set_queue(job_queue)
    yield job_queue
    set_queue(None)


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


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


@pytest.fixture
def pdf_upload():
    return SimpleUploadedFile("sample.pdf", make_pdf(), content_type="application/pdf")


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
