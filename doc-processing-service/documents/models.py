import uuid

from django.db import models


class Status(models.TextChoices):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Document(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField(help_text="Size in bytes")
    file_path = models.CharField(max_length=500)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Document({self.id}, {self.filename}, {self.status})"


class Job(models.Model):
    """One processing run of a document. `attempts` doubles as a fencing token (see worker)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="jobs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    error = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            # Used by the recovery sweep: "processing jobs started before X", "queued jobs untouched since X".
            models.Index(fields=["status", "started_at"], name="job_status_started_idx"),
            models.Index(fields=["status", "updated_at"], name="job_status_updated_idx"),
        ]

    def __str__(self) -> str:
        return f"Job({self.id}, {self.status}, attempt {self.attempts}/{self.max_attempts})"


class Result(models.Model):
    document = models.OneToOneField(Document, on_delete=models.CASCADE, primary_key=True, related_name="result")
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
