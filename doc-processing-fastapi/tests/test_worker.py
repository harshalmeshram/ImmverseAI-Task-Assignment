from datetime import timedelta

from sqlalchemy import update

from app.db import new_session
from app.models import Result, Status, Job, utcnow
from app.processing import LocalDocumentProcessor, PermanentProcessingError, ProcessingError

from .conftest import StubProcessor, get_document, get_job, new_job


def set_job(job_id, **values):
    with new_session() as session, session.begin():
        session.execute(update(Job).where(Job.id == job_id).values(**values))


def get_result(document_id):
    with new_session() as session:
        return session.get(Result, document_id)


def test_successful_processing_with_real_processor(make_worker, queue):
    job = new_job("Invoice number 42")
    worker = make_worker(LocalDocumentProcessor())

    assert worker.run_once() is True

    job = get_job(job.id)
    assert job.status == Status.COMPLETED
    assert job.attempts == 1
    assert job.started_at is not None and job.completed_at is not None
    assert job.error is None
    assert get_document(job.document_id).status == Status.COMPLETED
    assert "Invoice number 42" in get_result(job.document_id).text
    assert queue.size() == 0


def test_transient_failure_is_retried_then_succeeds(make_worker, queue):
    job = new_job()
    worker = make_worker(StubProcessor(ProcessingError("OCR service timeout"), "recovered text"))

    worker.run_once()
    current = get_job(job.id)
    assert current.status == Status.QUEUED
    assert current.attempts == 1
    assert "OCR service timeout" in current.error
    assert queue.size() == 1  # re-enqueued for the next attempt

    worker.run_once()
    current = get_job(job.id)
    assert current.status == Status.COMPLETED
    assert current.attempts == 2
    assert current.error is None
    assert get_result(job.document_id).text == "recovered text"


def test_job_fails_after_max_attempts(make_worker, queue):
    job = new_job()
    processor = StubProcessor(ProcessingError("boom"))
    worker = make_worker(processor)

    for _ in range(3):
        worker.run_once()

    current = get_job(job.id)
    assert current.status == Status.FAILED
    assert current.attempts == 3
    assert current.completed_at is not None
    assert "boom" in current.error
    assert get_document(job.document_id).status == Status.FAILED
    assert processor.calls == 3
    assert queue.size() == 0
    assert get_result(job.document_id) is None


def test_permanent_error_is_not_retried(make_worker, queue):
    job = new_job()
    make_worker(StubProcessor(PermanentProcessingError("corrupt file"))).run_once()

    current = get_job(job.id)
    assert current.status == Status.FAILED
    assert current.attempts == 1
    assert queue.size() == 0


def test_unexpected_exception_is_recorded_and_retried(make_worker):
    job = new_job()
    make_worker(StubProcessor(RuntimeError("unexpected"), "ok")).run_once()

    current = get_job(job.id)
    assert current.status == Status.QUEUED
    assert current.error == "RuntimeError: unexpected"


def test_retry_uses_exponential_backoff(make_worker, queue):
    new_job()
    make_worker(StubProcessor(ProcessingError("boom")), retry_backoff=60).run_once()

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
    current = get_job(job.id)
    assert current.status == Status.COMPLETED
    assert current.attempts == 1


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
    set_job(job.id, status=Status.PROCESSING, attempts=1, started_at=utcnow() - timedelta(hours=1))

    make_worker(StubProcessor("text")).recover_stale_jobs()

    current = get_job(job.id)
    assert current.status == Status.QUEUED
    assert "WorkerLostError" in current.error
    assert queue.size() == 1


def test_stale_job_on_last_attempt_is_failed(make_worker):
    job = new_job()
    set_job(job.id, status=Status.PROCESSING, attempts=3, started_at=utcnow() - timedelta(hours=1))

    make_worker(StubProcessor("text")).recover_stale_jobs()

    assert get_job(job.id).status == Status.FAILED
    assert get_document(job.document_id).status == Status.FAILED


def test_queued_job_without_message_is_reenqueued(make_worker, queue):
    """Simulates the API committing the job but failing to reach Redis."""
    job = new_job()
    queue.client.flushall()
    set_job(job.id, updated_at=utcnow() - timedelta(hours=1))

    worker = make_worker(StubProcessor("text"))
    worker.recover_stale_jobs()

    assert queue.size() == 1
    worker.run_once()
    assert get_job(job.id).status == Status.COMPLETED


def test_late_result_from_presumed_dead_worker_is_discarded(make_worker):
    job = new_job()
    slow_worker = make_worker(StubProcessor("stale text"))
    claimed = slow_worker.claim(str(job.id))

    # Meanwhile the sweep decided the slow worker was dead and requeued the job.
    set_job(job.id, status=Status.QUEUED)

    slow_worker.complete(claimed, "stale text")

    assert get_job(job.id).status == Status.QUEUED
    assert get_result(job.document_id) is None


def test_concurrent_workers_process_each_job_exactly_once(make_worker, queue):
    """Two workers drain the same queue in parallel; every job is also delivered twice."""
    import threading

    jobs = [new_job(f"document {i}") for i in range(8)]
    for job in jobs:
        queue.enqueue(str(job.id))  # duplicate message for every job

    calls: list[str] = []
    lock = threading.Lock()

    class CountingProcessor(StubProcessor):
        def process(self, file_path, content_type):
            with lock:
                calls.append(file_path)
            return "text"

    def drain(worker):
        while queue.size() > 0:
            worker.run_once()

    workers = [make_worker(CountingProcessor()) for _ in range(2)]
    threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(calls) == len(jobs)  # duplicates were skipped, nothing processed twice
    assert len(set(calls)) == len(jobs)
    for job in jobs:
        current = get_job(job.id)
        assert current.status == Status.COMPLETED
        assert current.attempts == 1
