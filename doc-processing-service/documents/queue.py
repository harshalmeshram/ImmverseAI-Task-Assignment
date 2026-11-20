"""A small Redis-backed job queue.

* `<name>:ready`   - LIST of job ids ready to run (LPUSH / BRPOP => FIFO).
* `<name>:delayed` - ZSET of job ids scored by the unix time they become runnable (retry backoff).

Messages only carry the job id; PostgreSQL stays the source of truth for job state. Delivery is
at-least-once, so consumers must be idempotent (the worker claims jobs with a conditional UPDATE).
"""

from __future__ import annotations

import time

import redis
from django.conf import settings


class JobQueue:
    def __init__(self, client: redis.Redis, name: str = "docproc:jobs") -> None:
        self.client = client
        self.ready_key = f"{name}:ready"
        self.delayed_key = f"{name}:delayed"

    def enqueue(self, job_id: str, delay: float = 0) -> None:
        if delay > 0:
            self.client.zadd(self.delayed_key, {job_id: time.time() + delay})
        else:
            self.client.lpush(self.ready_key, job_id)

    def dequeue(self, timeout: int = 2) -> str | None:
        """Block up to `timeout` seconds for the next job id."""
        self.promote_due()
        item = self.client.brpop([self.ready_key], timeout=timeout)
        return item[1] if item else None

    def promote_due(self, batch: int = 100) -> int:
        """Move delayed jobs whose time has come onto the ready list."""
        due = self.client.zrangebyscore(self.delayed_key, 0, time.time(), start=0, num=batch)
        moved = 0
        for job_id in due:
            # ZREM is atomic: if several workers race, only the one that removes it pushes it.
            if self.client.zrem(self.delayed_key, job_id):
                self.client.lpush(self.ready_key, job_id)
                moved += 1
        return moved

    def size(self) -> int:
        return self.client.llen(self.ready_key) + self.client.zcard(self.delayed_key)

    def ping(self) -> bool:
        return bool(self.client.ping())


_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True, health_check_interval=30)
        _queue = JobQueue(client, settings.QUEUE_NAME)
    return _queue


def set_queue(queue: JobQueue | None) -> None:
    """Swap the process-wide queue (used by tests)."""
    global _queue
    _queue = queue
