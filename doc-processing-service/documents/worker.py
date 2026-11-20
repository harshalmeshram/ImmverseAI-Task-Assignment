"""Background worker: consumes job ids from Redis and processes documents.

Correctness rests on three rules:

1. **Claim before work.** A job is claimed with a single conditional UPDATE
   (`status=queued -> processing`, `attempts += 1`). Only one worker can win that update, so
   duplicate messages and concurrent workers never process the same attempt twice.
2. **Fence every write.** Completing or failing a job is an UPDATE that also requires
   `status=processing AND attempts=<the attempt we claimed>`. If the job was meanwhile reclaimed
   (e.g. the sweep decided we were dead), our late write matches nothing and is discarded.
3. **The database is the source of truth, Redis is only a doorbell.** A periodic sweep re-enqueues
   `queued` jobs that lost their message and recovers `processing` jobs whose worker died.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from datetime import timedelta
from functools import partial

from django.db import DatabaseError, close_old_connections, transaction
from django.db.models import F
from django.utils import timezone
from redis.exceptions import RedisError

from .models import Document, Job, Result, Status
from .processing import DocumentProcessor, ProcessingError
from .queue import JobQueue
from .services import enqueue_job

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 2000


class WorkerLostError(ProcessingError):
    """Synthetic error recorded for a job whose worker stopped before finishing it."""


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        processor: DocumentProcessor,
        *,
        poll_timeout: int = 2,
        retry_backoff: float = 5,
        stale_after: float = 300,
        sweep_interval: float = 30,
        error_sleep: float = 2,
    ) -> None:
        self.queue = queue
        self.processor = processor
        self.poll_timeout = poll_timeout
        self.retry_backoff = retry_backoff
        self.stale_after = stale_after
        self.sweep_interval = sweep_interval
        self.error_sleep = error_sleep
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._stopping = False
        self._last_sweep: float | None = None

    # ------------------------------------------------------------------ main loop

    def stop(self, *_args) -> None:
        logger.info("worker %s: stop requested, finishing current job", self.worker_id)
        self._stopping = True

    def run(self) -> None:
        logger.info("worker %s started", self.worker_id)
        while not self._stopping:
            try:
                close_old_connections()  # drop DB connections that broke or expired
                self.run_once()
            except (RedisError, DatabaseError):
                logger.exception("worker %s: infrastructure error, backing off", self.worker_id)
                time.sleep(self.error_sleep)
            except Exception:
                logger.exception("worker %s: unexpected error", self.worker_id)
                time.sleep(self.error_sleep)
        logger.info("worker %s stopped", self.worker_id)

    def run_once(self) -> bool:
        """Sweep if due, then handle at most one message. Returns True if a message was received."""
        now = time.monotonic()
        if self._last_sweep is None or now - self._last_sweep >= self.sweep_interval:
            self._last_sweep = now
            self.recover_stale_jobs()

        job_id = self.queue.dequeue(timeout=self.poll_timeout)
        if job_id is None:
            return False
        self.handle(job_id)
        return True

    # ------------------------------------------------------------------ job lifecycle

    def handle(self, job_id: str) -> None:
        job = self._claim(job_id)
        if job is None:
            return

        logger.info("job %s: attempt %d/%d started by %s", job.id, job.attempts, job.max_attempts, self.worker_id)
        try:
            text = self.processor.process(job.document.file_path, job.document.content_type)
        except Exception as exc:  # any processor failure must end up recorded on the job
            logger.warning("job %s: attempt %d failed: %s", job.id, job.attempts, exc, exc_info=True)
            self._fail(job, exc)
            return
        self._complete(job, text)

    def _claim(self, job_id: str) -> Job | None:
        try:
            pk = uuid.UUID(str(job_id))
        except ValueError:
            logger.error("discarding malformed message %r", job_id)
            return None

        now = timezone.now()
        claimed = Job.objects.filter(pk=pk, status=Status.QUEUED, attempts__lt=F("max_attempts")).update(
            status=Status.PROCESSING,
            attempts=F("attempts") + 1,
            started_at=now,
            completed_at=None,
            updated_at=now,
        )
        if not claimed:
            logger.info("job %s: not claimable (duplicate message, already handled or unknown), skipping", job_id)
            return None

        job = Job.objects.select_related("document").get(pk=pk)
        Document.objects.filter(pk=job.document_id).update(status=Status.PROCESSING, updated_at=now)
        return job

    def _complete(self, job: Job, text: str) -> None:
        now = timezone.now()
        with transaction.atomic():
            updated = Job.objects.filter(pk=job.pk, status=Status.PROCESSING, attempts=job.attempts).update(
                status=Status.COMPLETED, completed_at=now, updated_at=now, error=None
            )
            if not updated:
                logger.warning("job %s: lost ownership of attempt %d, discarding result", job.id, job.attempts)
                return
            Result.objects.update_or_create(document_id=job.document_id, defaults={"text": text})
            Document.objects.filter(pk=job.document_id).update(status=Status.COMPLETED, updated_at=now)
        logger.info("job %s: completed (%d chars extracted)", job.id, len(text))

    def _fail(self, job: Job, exc: BaseException) -> None:
        now = timezone.now()
        error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
        retryable = getattr(exc, "retryable", True)  # unknown exceptions are assumed transient
        will_retry = retryable and job.attempts < job.max_attempts
        new_status = Status.QUEUED if will_retry else Status.FAILED

        with transaction.atomic():
            fields = {"status": new_status, "error": error, "updated_at": now}
            if not will_retry:
                fields["completed_at"] = now
            updated = Job.objects.filter(pk=job.pk, status=Status.PROCESSING, attempts=job.attempts).update(**fields)
            if not updated:
                logger.warning("job %s: lost ownership of attempt %d, not recording failure", job.id, job.attempts)
                return
            Document.objects.filter(pk=job.document_id).update(status=new_status, updated_at=now)
            if will_retry:
                delay = self.retry_backoff * 2 ** (job.attempts - 1)
                transaction.on_commit(partial(enqueue_job, job.pk, delay, self.queue))

        if will_retry:
            logger.info("job %s: will retry (attempt %d/%d used)", job.id, job.attempts, job.max_attempts)
        else:
            logger.error("job %s: failed permanently after %d attempt(s): %s", job.id, job.attempts, error)

    # ------------------------------------------------------------------ recovery

    def recover_stale_jobs(self, batch: int = 100) -> None:
        """Safe to run concurrently on every worker: all state changes are fenced/conditional."""
        cutoff = timezone.now() - timedelta(seconds=self.stale_after)

        # 1. Jobs stuck in `processing`: their worker crashed (or is hung). Count it as a failed attempt.
        stale = Job.objects.filter(status=Status.PROCESSING, started_at__lt=cutoff).order_by("started_at")[:batch]
        for job in stale:
            logger.warning("job %s: processing for longer than %ss, presuming worker lost", job.id, self.stale_after)
            self._fail(job, WorkerLostError(f"no result within {self.stale_after}s; worker presumed lost"))

        # 2. Jobs `queued` for a long time: their message may never have reached Redis. Re-enqueueing a job
        #    that *is* still in the queue is harmless: the second message fails to claim and is skipped.
        orphaned = Job.objects.filter(status=Status.QUEUED, updated_at__lt=cutoff).values_list("pk", flat=True)[:batch]
        for job_id in orphaned:
            if Job.objects.filter(pk=job_id, status=Status.QUEUED).update(updated_at=timezone.now()):
                logger.info("job %s: re-enqueueing queued job with no recent activity", job_id)
                enqueue_job(job_id, queue=self.queue)
