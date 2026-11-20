import signal

from django.conf import settings
from django.core.management.base import BaseCommand

from documents.processing import get_processor
from documents.queue import get_queue
from documents.worker import Worker


class Command(BaseCommand):
    help = "Run a document-processing worker. Start as many as you like; they coordinate via Redis + PostgreSQL."

    def handle(self, *args, **options) -> None:
        worker = Worker(
            queue=get_queue(),
            processor=get_processor(),
            poll_timeout=settings.WORKER_POLL_TIMEOUT_SECONDS,
            retry_backoff=settings.JOB_RETRY_BACKOFF_SECONDS,
            stale_after=settings.JOB_STALE_AFTER_SECONDS,
            sweep_interval=settings.WORKER_SWEEP_INTERVAL_SECONDS,
        )
        # Graceful shutdown: finish the current job, then exit (docker stop sends SIGTERM).
        signal.signal(signal.SIGTERM, worker.stop)
        signal.signal(signal.SIGINT, worker.stop)
        worker.run()
