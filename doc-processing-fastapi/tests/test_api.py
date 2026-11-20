import uuid
from pathlib import Path

import pytest
import redis

from app.config import settings
from app.db import new_session
from app.models import Document, Job, Result, Status
from app.queue import set_queue

from .conftest import get_document, make_pdf, make_png


def upload(client, name="sample.pdf", data=None, content_type="application/pdf"):
    data = make_pdf() if data is None else data
    return client.post("/documents", files={"file": (name, data, content_type)})


def count(model) -> int:
    with new_session() as session:
        return session.query(model).count()


class TestUpload:
    def test_pdf_upload_creates_document_and_job_and_enqueues(self, client, queue):
        response = upload(client)

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        with new_session() as session:
            document = session.get(Document, uuid.UUID(body["document_id"]))
            job = session.get(Job, uuid.UUID(body["job_id"]))
        assert job.document_id == document.id
        assert document.content_type == "application/pdf"
        assert document.filename == "sample.pdf"
        assert Path(document.file_path).is_file()
        assert response.headers["location"] == f"/documents/{document.id}"
        assert queue.dequeue(timeout=1) == str(job.id)

    def test_image_upload_is_accepted(self, client):
        response = upload(client, "scan.png", make_png(), "image/png")
        assert response.status_code == 202
        assert get_document(uuid.UUID(response.json()["document_id"])).content_type == "image/png"

    def test_type_is_detected_from_content_not_extension(self, client):
        response = upload(client, "fake.pdf", make_png(), "application/pdf")
        assert response.status_code == 202
        assert get_document(uuid.UUID(response.json()["document_id"])).content_type == "image/png"

    def test_redis_outage_does_not_fail_upload(self, client):
        class DownQueue:
            def enqueue(self, *args, **kwargs):
                raise redis.ConnectionError("redis is down")

        set_queue(DownQueue())
        response = upload(client)

        assert response.status_code == 202
        with new_session() as session:
            assert session.query(Job).one().status == Status.QUEUED  # recovery sweep will enqueue it later


class TestInvalidUpload:
    def test_missing_file(self, client):
        response = client.post("/documents")
        assert response.status_code == 400
        assert "file" in response.json()["detail"]

    def test_empty_file(self, client):
        assert upload(client, data=b"").status_code == 400

    def test_unsupported_type(self, client):
        assert upload(client, "notes.txt", b"just some text", "text/plain").status_code == 415

    def test_corrupt_pdf(self, client):
        response = upload(client, "bad.pdf", b"%PDF-1.7 garbage")
        assert response.status_code == 400
        assert "PDF" in response.json()["detail"]

    def test_corrupt_image(self, client):
        response = upload(client, "bad.png", b"\x89PNG\r\n\x1a\nnot really a png", "image/png")
        assert response.status_code == 400

    def test_too_large(self, client, monkeypatch):
        monkeypatch.setattr(settings, "max_upload_size", 10)
        assert upload(client).status_code == 413

    def test_rejected_upload_leaves_no_records(self, client):
        upload(client, "notes.txt", b"text", "text/plain")
        assert count(Document) == 0
        assert count(Job) == 0


class TestRead:
    def test_job_status(self, client):
        job_id = upload(client).json()["job_id"]
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == job_id
        assert body["status"] == "queued"
        assert body["attempts"] == 0
        assert body["max_attempts"] == 3
        assert body["error"] is None
        assert body["started_at"] is None

    def test_document_without_result(self, client):
        body = upload(client).json()
        data = client.get(f"/documents/{body['document_id']}").json()
        assert data["status"] == "queued"
        assert data["result"] is None
        assert data["latest_job"]["id"] == body["job_id"]

    def test_document_with_result(self, client):
        document_id = uuid.UUID(upload(client).json()["document_id"])
        with new_session() as session, session.begin():
            session.get(Document, document_id).status = Status.COMPLETED
            session.add(Result(document_id=document_id, text="Hello World"))

        data = client.get(f"/documents/{document_id}").json()
        assert data["status"] == "completed"
        assert data["result"]["text"] == "Hello World"

    @pytest.mark.parametrize("path", ["/documents/{}", "/jobs/{}"])
    def test_missing_returns_404(self, client, path):
        response = client.get(path.format(uuid.uuid4()))
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_malformed_id_is_rejected(self, client):
        assert client.get("/jobs/not-a-uuid").status_code == 422


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


def test_timestamps_are_utc_iso8601(client):
    job_id = upload(client).json()["job_id"]
    created_at = client.get(f"/jobs/{job_id}").json()["created_at"]
    assert created_at.endswith("Z")
