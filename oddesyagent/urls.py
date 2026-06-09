from django.contrib import admin
from django.urls import path

from apps.core.views import (
    InternalJobDetailView,
    InternalJobOutputView,
    InternalJobsView,
    InternalMediaListView,
    InternalWorkflowsView,
)


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/internal/workflows/", InternalWorkflowsView.as_view(), name="internal-workflows"),
    path("api/internal/jobs/", InternalJobsView.as_view(), name="internal-jobs"),
    path("api/internal/jobs/<int:job_id>/", InternalJobDetailView.as_view(), name="internal-job-detail"),
    path("api/internal/jobs/<int:job_id>/output/", InternalJobOutputView.as_view(), name="internal-job-output"),
    path("api/internal/media/", InternalMediaListView.as_view(), name="internal-media-list"),
]
