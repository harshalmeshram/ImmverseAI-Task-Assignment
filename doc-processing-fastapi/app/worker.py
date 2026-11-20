"""Background worker: consumes job ids from Redis and processes documents.

Run with `python -m app.worker`; start as many as you like.

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
import signal
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload

from .config import settings
from .db import new_session
from .models import Document, Job, Result, Status, utcnow
from .processing import DocumentProcessor, ProcessingError, get_processor
from .queue import JobQueue, get_queue
from .services import enqueue_job

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 2000


class WorkerLostError(ProcessingError):
    """Synthetic error recorded for a job whose worker stopped before finishing it."""


@dataclass(frozen=True)
class ClaimedJob:
    """Snapshot of a job taken at claim time; `attempts` is the fencing token for later writes."""

    id: uuid.UUID
    document_id: uuid.UUID
    attempts: int
    max_attempts: int
    file_path: str
    content_type: str

    @classmethod
    def from_model(cls, job: Job) -> ClaimedJob:
        return cls(
            id=job.id,
            document_id=job.document_id,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            file_path=job.document.file_path,
            content_type=job.document.content_type,
        )


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
                self.run_once()
            except (RedisError, SQLAlchemyError):
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
        job = self.claim(job_id)
        if job is None:
            return

        logger.info("job %s: attempt %d/%d started by %s", job.id, job.attempts, job.max_attempts, self.worker_id)
        try:
            text = self.processor.process(job.file_path, job.content_type)
        except Exception as exc:  # any processor failure must end up recorded on the job
            logger.warning("job %s: attempt %d failed: %s", job.id, job.attempts, exc, exc_info=True)
            self.fail(job, exc)
            return
        self.complete(job, text)

    def claim(self, job_id: str) -> ClaimedJob | None:
        try:
            pk = uuid.UUID(str(job_id))
        except ValueError:
            logger.error("discarding malformed message %r", job_id)
            return None

        now = utcnow()
        with new_session() as session, session.begin():
            claimed = session.execute(
                update(Job)
                .where(Job.id == pk, Job.status == Status.QUEUED, Job.attempts < Job.max_attempts)
                .values(
                    status=Status.PROCESSING,
                    attempts=Job.attempts + 1,
                    started_at=now,
                    completed_at=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != 1:
                logger.info("job %s: not claimable (duplicate message, already handled or unknown), skipping", job_id)
                return None

            job = session.scalars(select(Job).options(joinedload(Job.document)).where(Job.id == pk)).one()
            job.document.status = Status.PROCESSING
            job.document.updated_at = now
            return ClaimedJob.from_model(job)

    def complete(self, job: ClaimedJob, text: str) -> None:
        now = utcnow()
        with new_session() as session, session.begin():
            updated = session.execute(
                update(Job)
                .where(Job.id == job.id, Job.status == Status.PROCESSING, Job.attempts == job.attempts)
                .values(status=Status.COMPLETED, completed_at=now, updated_at=now, error=None)
                .execution_options(synchronize_session=False)
            ).rowcount
            if updated != 1:
                logger.warning("job %s: lost ownership of attempt %d, discarding result", job.id, job.attempts)
                return
            session.merge(Result(document_id=job.document_id, text=text, created_at=now))
            session.execute(
                update(Document).where(Document.id == job.document_id).values(status=Status.COMPLETED, updated_at=now)
            )
        logger.info("job %s: completed (%d chars extracted)", job.id, len(text))

    def fail(self, job: ClaimedJob, exc: BaseException) -> None:
        now = utcnow()
        error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
        retryable = getattr(exc, "retryable", True)  # unknown exceptions are assumed transient
        will_retry = retryable and job.attempts < job.max_attempts
        new_status = Status.QUEUED if will_retry else Status.FAILED

        values: dict = {"status": new_status, "error": error, "updated_at": now}
        if not will_retry:
            values["completed_at"] = now

        with new_session() as session, session.begin():
            updated = session.execute(
                update(Job)
                .where(Job.id == job.id, Job.status == Status.PROCESSING, Job.attempts == job.attempts)
                .values(**values)
                .execution_options(synchronize_session=False)
            ).rowcount
            if updated != 1:
                logger.warning("job %s: lost ownership of attempt %d, not recording failure", job.id, job.attempts)
                return
            session.execute(
                update(Document).where(Document.id == job.document_id).values(status=new_status, updated_at=now)
            )

        # Committed: only now make the retry visible to other workers.
        if will_retry:
            delay = self.retry_backoff * 2 ** (job.attempts - 1)
            enqueue_job(job.id, delay, self.queue)
            logger.info("job %s: will retry in %.0fs (attempt %d/%d used)", job.id, delay, job.attempts, job.max_attempts)
        else:
            logger.error("job %s: failed permanently after %d attempt(s): %s", job.id, job.attempts, error)

    # ------------------------------------------------------------------ recovery

    def recover_stale_jobs(self, batch: int = 100) -> None:
        """Safe to run concurrently on every worker: all state changes are fenced/conditional."""
        cutoff = utcnow() - timedelta(seconds=self.stale_after)

        with new_session() as session:
            # 1. Jobs stuck in `processing`: their worker crashed (or is hung). Count it as a failed attempt.
            stale = [
                ClaimedJob.from_model(job)
                for job in session.scalars(
                    select(Job)
                    .options(joinedload(Job.document))
                    .where(Job.status == Status.PROCESSING, Job.started_at < cutoff)
                    .order_by(Job.started_at)
                    .limit(batch)
                )
            ]
            # 2. Jobs `queued` for a long time: their message may never have reached Redis.
            orphaned = list(
                session.scalars(
                    select(Job.id).where(Job.status == Status.QUEUED, Job.updated_at < cutoff).limit(batch)
                )
            )

        for job in stale:
            logger.warning("job %s: processing for longer than %ss, presuming worker lost", job.id, self.stale_after)
            self.fail(job, WorkerLostError(f"no result within {self.stale_after}s; worker presumed lost"))

        # Re-enqueueing a job that *is* still in the queue is harmless: the extra message fails to claim.
        for job_id in orphaned:
            with new_session() as session, session.begin():
                touched = session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == Status.QUEUED)
                    .values(updated_at=utcnow())
                    .execution_options(synchronize_session=False)
                ).rowcount
            if touched:
                logger.info("job %s: re-enqueueing queued job with no recent activity", job_id)
                enqueue_job(job_id, queue=self.queue)


def main() -> None:
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    worker = Worker(
        queue=get_queue(),
        processor=get_processor(),
        poll_timeout=settings.worker_poll_timeout_seconds,
        retry_backoff=settings.job_retry_backoff_seconds,
        stale_after=settings.job_stale_after_seconds,
        sweep_interval=settings.worker_sweep_interval_seconds,
    )
    # Graceful shutdown: finish the current job, then exit (docker stop sends SIGTERM).
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run()


if __name__ == "__main__":
    main()
