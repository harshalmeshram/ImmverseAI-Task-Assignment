from django.urls import path

from documents import views

urlpatterns = [
    path("documents", views.DocumentCreateView.as_view(), name="document-create"),
    path("documents/<uuid:document_id>", views.DocumentDetailView.as_view(), name="document-detail"),
    path("jobs/<uuid:job_id>", views.JobDetailView.as_view(), name="job-detail"),
    path("health", views.health, name="health"),
]

handler404 = "documents.views.not_found"
handler500 = "documents.views.server_error"
