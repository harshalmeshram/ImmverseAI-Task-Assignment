from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from documents.models import Document, Job, Result, Status
from documents.processing import LocalDocumentProcessor, PermanentProcessingError, ProcessingError
from documents.services import create_document

from .conftest import StubProcessor, make_pdf

# transaction=True so that transaction.on_commit() callbacks (enqueueing) really run.
pytestmark = pytest.mark.django_db(transaction=True)


def new_job(text: str = "Hello World") -> Job:
    _, job = create_document(SimpleUploadedFile("doc.pdf", make_pdf(text), content_type="application/pdf"))
    return job


def test_successful_processing_with_real_processor(make_worker, queue):
    job = new_job("Invoice number 42")
    worker = make_worker(LocalDocumentProcessor())

    assert worker.run_once() is True

    job.refresh_from_db()
    assert job.status == Status.COMPLETED
    assert job.attempts == 1
    assert job.started_at is not None and job.completed_at is not None
    assert job.error is None
    assert job.document.status == Status.COMPLETED
    assert "Invoice number 42" in Result.objects.get(document=job.document).text
    assert queue.size() == 0


def test_transient_failure_is_retried_then_succeeds(make_worker, queue):
    job = new_job()
    processor = StubProcessor(ProcessingError("OCR service timeout"), "recovered text")
    worker = make_worker(processor)

    worker.run_once()
    job.refresh_from_db()
    assert job.status == Status.QUEUED
    assert job.attempts == 1
    assert "OCR service timeout" in job.error
    assert queue.size() == 1  # re-enqueued for the next attempt

    worker.run_once()
    job.refresh_from_db()
    assert job.status == Status.COMPLETED
    assert job.attempts == 2
    assert job.error is None
    assert Result.objects.get(document=job.document).text == "recovered text"


def test_job_fails_after_max_attempts(make_worker, queue):
    job = new_job()
    processor = StubProcessor(ProcessingError("boom"))
    worker = make_worker(processor)

    for _ in range(3):
        worker.run_once()

    job.refresh_from_db()
    assert job.status == Status.FAILED
    assert job.attempts == 3
    assert job.completed_at is not None
    assert "boom" in job.error
    assert job.document.status == Status.FAILED
    assert processor.calls == 3
    assert queue.size() == 0
    assert not Result.objects.exists()


def test_permanent_error_is_not_retried(make_worker, queue):
    job = new_job()
    worker = make_worker(StubProcessor(PermanentProcessingError("corrupt file")))

    worker.run_once()

    job.refresh_from_db()
    assert job.status == Status.FAILED
    assert job.attempts == 1
    assert queue.size() == 0


def test_unexpected_exception_is_recorded_and_retried(make_worker):
    job = new_job()
    worker = make_worker(StubProcessor(RuntimeError("unexpected"), "ok"))

    worker.run_once()
    job.refresh_from_db()
    assert job.status == Status.QUEUED
    assert job.error == "RuntimeError: unexpected"


def test_retry_uses_exponential_backoff(make_worker, queue):
    new_job()
    worker = make_worker(StubProcessor(ProcessingError("boom")), retry_backoff=60)

    worker.run_once()

    assert queue.client.llen(queue.ready_key) == 0
    assert queue.client.zcard(queue.delayed_key) == 1  # waiting in the delayed set, not runnable yet


def test_duplicate_messages_are_processed_once(make_worker, queue):
    job = new_job()
    queue.enqueue(str(job.id))  # same job delivered twice
    processor = StubProcessor("text")
    worker = make_worker(processor)

    worker.run_once()
    worker.run_once()

    assert processor.calls == 1
    job.refresh_from_db()
    assert job.status == Status.COMPLETED
    assert job.attempts == 1


def test_malformed_and_unknown_messages_are_ignored(make_worker, queue):
    queue.enqueue("not-a-uuid")
    queue.enqueue("6f1c1d1e-0000-4000-8000-000000000000")
    processor = StubProcessor("text")
    worker = make_worker(processor)

    worker.run_once()
    worker.run_once()

    assert processor.calls == 0


def test_stale_processing_job_is_recovered(make_worker, queue):
    """Simulates a worker that crashed mid-job: the job is left in `processing`."""
    job = new_job()
    queue.client.flushall()
    Job.objects.filter(pk=job.pk).update(
        status=Status.PROCESSING, attempts=1, started_at=timezone.now() - timedelta(hours=1)
    )

    make_worker(StubProcessor("text")).recover_stale_jobs()

    job.refresh_from_db()
    assert job.status == Status.QUEUED
    assert "WorkerLostError" in job.error
    assert queue.size() == 1


def test_stale_job_on_last_attempt_is_failed(make_worker, queue):
    job = new_job()
    Job.objects.filter(pk=job.pk).update(
        status=Status.PROCESSING, attempts=3, started_at=timezone.now() - timedelta(hours=1)
    )

    make_worker(StubProcessor("text")).recover_stale_jobs()

    job.refresh_from_db()
    assert job.status == Status.FAILED
    assert Document.objects.get(pk=job.document_id).status == Status.FAILED


def test_queued_job_without_message_is_reenqueued(make_worker, queue):
    """Simulates the API committing the job but failing to reach Redis."""
    job = new_job()
    queue.client.flushall()
    Job.objects.filter(pk=job.pk).update(updated_at=timezone.now() - timedelta(hours=1))

    worker = make_worker(StubProcessor("text"))
    worker.recover_stale_jobs()

    assert queue.size() == 1
    worker.run_once()
    job.refresh_from_db()
    assert job.status == Status.COMPLETED


def test_late_result_from_presumed_dead_worker_is_discarded(make_worker):
    job = new_job()
    slow_worker = make_worker(StubProcessor("stale text"))
    claimed = slow_worker._claim(str(job.id))

    # Meanwhile the sweep decided the slow worker was dead and requeued the job.
    Job.objects.filter(pk=job.pk).update(status=Status.QUEUED)

    slow_worker._complete(claimed, "stale text")

    job.refresh_from_db()
    assert job.status == Status.QUEUED
    assert not Result.objects.exists()
