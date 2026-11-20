import logging

from django.db import connection
from django.http import JsonResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.generics import RetrieveAPIView
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Document, Job
from .queue import get_queue
from .serializers import DocumentSerializer, JobSerializer
from .services import create_document

logger = logging.getLogger(__name__)


class DocumentCreateView(APIView):
    """POST /documents - accept a PDF/image (multipart field `file`) and queue it for processing."""

    parser_classes = [MultiPartParser]

    def post(self, request: Request) -> Response:
        document, job = create_document(request.FILES.get("file"))
        return Response(
            {"document_id": str(document.id), "job_id": str(job.id), "status": job.status},
            status=status.HTTP_202_ACCEPTED,
            headers={"Location": f"/documents/{document.id}"},
        )


class DocumentDetailView(RetrieveAPIView):
    """GET /documents/{document_id} - document metadata, latest job and extracted text (once done)."""

    queryset = Document.objects.select_related("result")
    serializer_class = DocumentSerializer
    lookup_url_kwarg = "document_id"


class JobDetailView(RetrieveAPIView):
    """GET /jobs/{job_id} - job status, attempts, timestamps and last error."""

    queryset = Job.objects.all()
    serializer_class = JobSerializer
    lookup_url_kwarg = "job_id"


@api_view(["GET"])
def health(request: Request) -> Response:
    checks = {}
    try:
        connection.ensure_connection()
        checks["database"] = "ok"
    except Exception:
        logger.exception("health check: database unavailable")
        checks["database"] = "unavailable"
    try:
        get_queue().ping()
        checks["redis"] = "ok"
    except Exception:
        logger.exception("health check: redis unavailable")
        checks["redis"] = "unavailable"

    healthy = all(value == "ok" for value in checks.values())
    return Response(
        {"status": "ok" if healthy else "degraded", **checks},
        status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def not_found(request, exception=None) -> JsonResponse:
    return JsonResponse({"detail": "Not found."}, status=404)


def server_error(request) -> JsonResponse:
    return JsonResponse({"detail": "Internal server error."}, status=500)
