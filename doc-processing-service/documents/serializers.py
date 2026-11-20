from rest_framework import serializers

from .models import Document, Job, Result


class JobSerializer(serializers.ModelSerializer):
    document_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Job
        fields = [
            "id",
            "document_id",
            "status",
            "attempts",
            "max_attempts",
            "error",
            "created_at",
            "started_at",
            "completed_at",
            "updated_at",
        ]


class ResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = Result
        fields = ["text", "created_at"]


class DocumentSerializer(serializers.ModelSerializer):
    latest_job = serializers.SerializerMethodField()
    result = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id",
            "filename",
            "content_type",
            "size",
            "status",
            "created_at",
            "updated_at",
            "latest_job",
            "result",
        ]

    def get_latest_job(self, document: Document) -> dict | None:
        job = document.jobs.order_by("-created_at").first()
        return JobSerializer(job).data if job else None

    def get_result(self, document: Document) -> dict | None:
        try:
            return ResultSerializer(document.result).data
        except Result.DoesNotExist:
            return None
