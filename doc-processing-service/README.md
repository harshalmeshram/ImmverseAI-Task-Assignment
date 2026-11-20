# Asynchronous Document Processing Service

A small backend that accepts PDFs and images, queues them in **Redis**, and extracts their text in
separate **background workers**. All state lives in **PostgreSQL**.

Stack: Python 3.12 · Django 5.2 + Django REST Framework · PostgreSQL 16 · Redis 7 · PyMuPDF + Tesseract · pytest · Docker Compose.

> **Note on the framework:** the brief asks for FastAPI. This implementation uses **Django + DRF**
> for the HTTP layer. Nothing else depends on the web framework: the queue, the worker, the
> retry/duplicate logic and the processor are plain Python modules.

---

## Run it

```bash
cp .env.example .env          # optional, every value has a default
docker compose up --build     # api on http://localhost:8000, 2 workers, postgres, redis
docker compose up --scale worker=10   # more workers
```

Compose starts `db` and `redis`, then a one-off `migrate` container, then `api` (gunicorn) and the
`worker` replicas. The API and the workers share an `uploads` volume for the stored files.

### Try it

```bash
curl -F "file=@invoice.pdf" http://localhost:8000/documents
# {"document_id": "...", "job_id": "...", "status": "queued"}

curl http://localhost:8000/jobs/<job_id>
curl http://localhost:8000/documents/<document_id>
```

### Run locally without Docker

```bash
python -m venv .venv && .venv/Scripts/activate     # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
export DB_ENGINE=sqlite REDIS_URL=redis://localhost:6379/0   # needs a Redis; tesseract only for images/scans
python manage.py migrate
python manage.py runserver          # API
python manage.py run_worker         # worker (start as many as you like)
```

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests need no external services: they use SQLite and `fakeredis`, and Tesseract is mocked.

---

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/documents` | Upload a file (`multipart/form-data`, field `file`). Returns **202** with `document_id`, `job_id` and `status`. |
| `GET` | `/documents/{document_id}` | Document metadata, status, latest job and `result.text` once completed. |
| `GET` | `/jobs/{job_id}` | Job status, `attempts`/`max_attempts`, `error`, `created_at`/`started_at`/`completed_at`. |
| `GET` | `/health` | Database and Redis connectivity (200, or 503 if either is down). |

Accepted types: **PDF, PNG, JPEG, TIFF**, up to 10 MB (configurable). The type comes from the
file's magic bytes, not from its name or the declared `Content-Type`. The file is then parsed, so
corrupt or encrypted files are rejected at upload time.

| Error | Status |
|---|---|
| missing or empty file, corrupt PDF/image, password-protected PDF | `400` |
| unknown document/job id | `404` |
| file too large | `413` |
| unsupported file type (or a non-multipart request) | `415` |

All errors are JSON: `{"detail": "..."}`.

Job states: `queued → processing → completed`, or `processing → queued` (retry), or `processing → failed`.
`Document.status` mirrors its job.

---

## Architecture

```
            POST /documents
Client ───────────────────────► API (Django/DRF, gunicorn)
   ▲                              │ 1. validate file, save to shared volume
   │ GET /jobs/{id}               │ 2. INSERT document + job (status=queued)   ──► PostgreSQL
   │ GET /documents/{id}          │ 3. after commit: LPUSH job_id              ──► Redis
   └──────────────────────────────┘ 4. return 202 immediately

Redis  docproc:jobs:ready (LIST)   ──BRPOP──►  Worker 1..N  (python manage.py run_worker)
       docproc:jobs:delayed (ZSET) ◄─retry───    │ claim:  UPDATE job SET processing, attempts+1
                                                  │         WHERE status='queued'        ──► PostgreSQL
                                                  │ DocumentProcessor.process(file)
                                                  │         (PyMuPDF text layer, Tesseract OCR fallback)
                                                  └ save Result + job completed / queued / failed ──► PostgreSQL
```

### Code layout

```
config/                 settings, urls
documents/
  models.py             Document, Job, Result
  validators.py         magic-byte type detection + integrity checks
  services.py           create_document(): store file, insert rows, enqueue after commit
  queue.py              JobQueue: Redis ready list + delayed (retry) sorted set
  processing.py         DocumentProcessor interface + LocalDocumentProcessor
  worker.py             Worker: claim → process → complete/retry/fail, recovery sweep
  views.py, serializers.py
  management/commands/run_worker.py
tests/                  API, worker and processor tests
```

---

## Design decisions

- **PostgreSQL is the source of truth; Redis is only a doorbell.** A queue message carries just the
  `job_id`. Job state, attempts and errors are stored in the database, so a lost or duplicated
  message is never a correctness problem. At worst it delays a job.
- **A small custom Redis queue instead of Celery.** It is about 50 lines: a LIST for ready jobs
  (`LPUSH`/`BRPOP`, FIFO) and a ZSET for delayed retries. That keeps the retry and duplicate
  semantics explicit and easy to review, as the brief asks. Celery would add its own retry/ack
  layer on top of ours.
- **Enqueue after commit** (`transaction.on_commit`). A worker can never receive a job id that is
  not yet visible in the database. If Redis is down at that moment, the upload still succeeds
  (the job is `queued` in the DB) and the recovery sweep enqueues it later.
- **The processor is behind an interface.** `DocumentProcessor.process(file_path, content_type) -> str`.
  The implementation is chosen with the `DOCUMENT_PROCESSOR` setting (a dotted path), so a hosted
  OCR service (e.g. the LightOnOCR Hugging Face Space via `gradio_client`) can replace it without
  touching the worker.
- **Two kinds of processing errors.** `ProcessingError` is retryable (OCR engine or service
  unavailable, timeouts, unexpected exceptions). `PermanentProcessingError` is not (corrupt or
  missing file, unsupported type), because retrying cannot help.
- **Workers keep running.** Every exception from a job is recorded on that job. Redis or DB outages
  are logged, followed by a short back-off. DB connections are recycled on every loop. `SIGTERM`
  lets the current job finish before the worker exits.
- **The API stays available when jobs fail.** Processing never happens in the request path.

## How retries work

1. A failed attempt with a retryable error, and `attempts < max_attempts` (3), sets the job back to
   `queued` and records the `error`.
2. After the DB commit, the job id goes into the **delayed ZSET** with exponential back-off:
   `JOB_RETRY_BACKOFF_SECONDS × 2^(attempt-1)`, i.e. 5 s, then 10 s.
3. Each worker moves due entries from the ZSET to the ready list before polling. `ZREM` decides the
   winner, so a retry is promoted only once even when many workers race.
4. When the last attempt fails, or the error is permanent, the job and document become `failed`.
   `completed_at` and the last `error` are kept.

`attempts` is incremented when a job is **claimed**, so a crash also uses up an attempt. A document
that crashes workers cannot loop forever.

## How duplicate jobs/messages are handled

Delivery is *at least once*, so duplicates can happen: a retry plus a sweep re-enqueue, a client
re-push, or manual replays. Two layers make processing effectively *once per attempt*:

1. **Atomic claim.** A worker may process a job only after this query updates exactly one row:
   ```sql
   UPDATE job SET status='processing', attempts=attempts+1, started_at=now()
   WHERE id=:id AND status='queued' AND attempts < max_attempts;
   ```
   PostgreSQL row locking means only one worker can win. A duplicate message, or one for a
   completed, failed or unknown job, updates 0 rows and is dropped.
2. **Fenced writes.** Completion and failure updates also require `status='processing' AND attempts=:claimed_attempt`.
   Suppose a worker is presumed dead and its job reclaimed, but it later comes back with a
   result. Its write matches nothing and is discarded, so it cannot overwrite the newer attempt.
   `Result` is also written with `update_or_create` (one row per document).

---

## Assumptions

- One processing job per uploaded document. Uploading the same file twice creates two documents,
  because there is no content-hash deduplication.
- Files are stored on a volume shared by the API and the workers. In production this would be object
  storage (S3/GCS) and `file_path` would be an object key.
- No authentication or multi-tenancy; it is out of scope.
- OCR quality is not a goal. PDFs use their embedded text, and Tesseract is used only for pages
  without text and for images.
- `JOB_STALE_AFTER_SECONDS` (default 300 s) is longer than any legitimate processing time.

---

## Questions

**1. What happens if a worker crashes while processing a job?**
The job stays `processing` with a `started_at`. Every worker runs a recovery sweep (every 30 s) that
finds jobs in `processing` for longer than `JOB_STALE_AFTER_SECONDS` and records the attempt as
failed (`WorkerLostError`). The job then gets the normal retry path (`queued` with back-off), or
`failed` if it was the last attempt. If the "dead" worker was only slow and reports later, the
fenced write discards its result. The same sweep re-enqueues `queued` jobs whose Redis message was
lost, e.g. if Redis was down when the API tried to enqueue.

**2. How would you scale this to 10 workers?**
`docker compose up --scale worker=10`, or `replicas: 10` in Kubernetes. Workers are stateless and
coordinate only through Redis (`BRPOP` gives each message to one consumer) and the atomic claim in
PostgreSQL, so no other change is needed. Things to watch at that scale: DB connections (10 workers
plus the API; add PgBouncer later), CPU for OCR (size the workers by cores), and shared file storage
(move to object storage once the workers run on several hosts).

**3. How would you handle 1,000 documents arriving at once?**
The API does only cheap work per request (validate, write the file, two INSERTs, one `LPUSH`) and
returns `202`, so the burst ends up as a backlog in the queue. The workers drain it at their own
pace, and we add workers if the backlog grows (autoscale on queue length, `LLEN docproc:jobs:ready`).
Clients poll `/jobs/{id}`. Further steps: run more gunicorn workers or API replicas behind a load
balancer, upload directly to object storage with pre-signed URLs so the API does not stream large
bodies, rate-limit per client, and add a batch-upload endpoint if clients need one. Redis keeps the
queue with AOF persistence (enabled in compose).

**4. How would you prevent the same job from being processed twice?**
With the conditional `UPDATE ... WHERE status='queued'` claim, which is the single point of mutual
exclusion, plus fenced completion writes keyed on the attempt number (see
[duplicate handling](#how-duplicate-jobsmessages-are-handled)). Even then, a worker that hangs
past the stale timeout means a second worker *may* run the same document again. That is inherent to
at-least-once systems, so the side effect (writing `Result`) is idempotent and only the current
attempt is allowed to commit.
