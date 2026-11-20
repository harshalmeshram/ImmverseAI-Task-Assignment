import uuid
from pathlib import Path

import pytest
import redis
from django.core.files.uploadedfile import SimpleUploadedFile

from documents.models import Document, Job, Result, Status
from documents.queue import set_queue

from .conftest import make_png

pytestmark = pytest.mark.django_db


def upload(client, file):
    return client.post("/documents", {"file": file}, format="multipart")


class TestUpload:
    def test_pdf_upload_creates_document_and_job_and_enqueues(
        self, api_client, pdf_upload, queue, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            response = upload(api_client, pdf_upload)

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        document = Document.objects.get(pk=body["document_id"])
        job = Job.objects.get(pk=body["job_id"])
        assert job.document_id == document.id
        assert document.content_type == "application/pdf"
        assert document.filename == "sample.pdf"
        assert document.size == pdf_upload.size
        assert Path(document.file_path).is_file()
        assert response["Location"] == f"/documents/{document.id}"
        assert queue.dequeue(timeout=1) == str(job.id)

    def test_image_upload_is_accepted(self, api_client):
        response = upload(api_client, SimpleUploadedFile("scan.png", make_png(), content_type="image/png"))
        assert response.status_code == 202
        assert Document.objects.get().content_type == "image/png"

    def test_type_is_detected_from_content_not_extension(self, api_client):
        # A PNG named .pdf with a lying content type is still stored as a PNG.
        response = upload(api_client, SimpleUploadedFile("fake.pdf", make_png(), content_type="application/pdf"))
        assert response.status_code == 202
        assert Document.objects.get().content_type == "image/png"

    def test_redis_outage_does_not_fail_upload(self, api_client, pdf_upload, django_capture_on_commit_callbacks):
        class DownQueue:
            def enqueue(self, *args, **kwargs):
                raise redis.ConnectionError("redis is down")

        set_queue(DownQueue())
        with django_capture_on_commit_callbacks(execute=True):
            response = upload(api_client, pdf_upload)

        assert response.status_code == 202
        assert Job.objects.get().status == Status.QUEUED  # recovery sweep will enqueue it later


class TestInvalidUpload:
    def test_missing_file(self, api_client):
        response = api_client.post("/documents", {}, format="multipart")
        assert response.status_code == 400
        assert "file" in response.json()["detail"]

    def test_empty_file(self, api_client):
        response = upload(api_client, SimpleUploadedFile("empty.pdf", b"", content_type="application/pdf"))
        assert response.status_code == 400

    def test_unsupported_type(self, api_client):
        response = upload(api_client, SimpleUploadedFile("notes.txt", b"just some text", content_type="text/plain"))
        assert response.status_code == 415

    def test_corrupt_pdf(self, api_client):
        response = upload(api_client, SimpleUploadedFile("bad.pdf", b"%PDF-1.7 garbage", content_type="application/pdf"))
        assert response.status_code == 400
        assert "PDF" in response.json()["detail"]

    def test_corrupt_image(self, api_client):
        data = b"\x89PNG\r\n\x1a\n" + b"not really a png"
        response = upload(api_client, SimpleUploadedFile("bad.png", data, content_type="image/png"))
        assert response.status_code == 400

    def test_too_large(self, api_client, pdf_upload, settings):
        settings.MAX_UPLOAD_SIZE = 10
        response = upload(api_client, pdf_upload)
        assert response.status_code == 413

    def test_rejected_upload_leaves_no_records(self, api_client):
        upload(api_client, SimpleUploadedFile("notes.txt", b"text", content_type="text/plain"))
        assert not Document.objects.exists()
        assert not Job.objects.exists()

    def test_json_body_is_rejected(self, api_client):
        response = api_client.post("/documents", {"file": "x"}, format="json")
        assert response.status_code == 415


class TestRead:
    def test_job_status(self, api_client, pdf_upload):
        job_id = upload(api_client, pdf_upload).json()["job_id"]
        response = api_client.get(f"/jobs/{job_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == job_id
        assert body["status"] == "queued"
        assert body["attempts"] == 0
        assert body["max_attempts"] == 3
        assert body["error"] is None
        assert body["started_at"] is None

    def test_document_without_result(self, api_client, pdf_upload):
        body = upload(api_client, pdf_upload).json()
        response = api_client.get(f"/documents/{body['document_id']}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["result"] is None
        assert data["latest_job"]["id"] == body["job_id"]

    def test_document_with_result(self, api_client, pdf_upload):
        document_id = upload(api_client, pdf_upload).json()["document_id"]
        Document.objects.filter(pk=document_id).update(status=Status.COMPLETED)
        Result.objects.create(document_id=document_id, text="Hello World")

        data = api_client.get(f"/documents/{document_id}").json()
        assert data["status"] == "completed"
        assert data["result"]["text"] == "Hello World"

    @pytest.mark.parametrize("path", ["/documents/{}", "/jobs/{}"])
    def test_missing_returns_404(self, api_client, path):
        response = api_client.get(path.format(uuid.uuid4()))
        assert response.status_code == 404
        assert "detail" in response.json()

    def test_malformed_id_returns_404(self, api_client):
        assert api_client.get("/jobs/not-a-uuid").status_code == 404


def test_health(api_client):
    response = api_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}
