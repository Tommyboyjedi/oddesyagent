from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import GenerationJob
from apps.core.services.job_service import JobService


class Command(BaseCommand):
    help = "Inspect, cancel, or retry generation jobs."

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.job_service = JobService()

    def add_arguments(self, parser) -> None:
        parser.add_argument("--limit", type=int, default=10, help="Number of recent jobs to list.")
        parser.add_argument("--job-id", type=int, help="Show details for one job.")
        parser.add_argument("--cancel", type=int, help="Cancel or request cancellation for a job.")
        parser.add_argument("--retry", type=int, help="Retry a terminal job by creating a new queued job.")

    def handle(self, *args, **options) -> None:
        if options["cancel"]:
            self._cancel_job(options["cancel"])
            return
        if options["retry"]:
            self._retry_job(options["retry"])
            return
        if options["job_id"]:
            self._show_job(options["job_id"])
            return
        self._list_jobs(options["limit"])

    def _list_jobs(self, limit: int) -> None:
        jobs = (
            GenerationJob.objects.select_related("telegram_user", "input_media", "output_media")
            .order_by("-created_at")[:limit]
        )
        if not jobs:
            self.stdout.write("No jobs found.")
            return

        for job in jobs:
            failure_type = job.metadata.get("failure", {}).get("failure_type", "-")
            output_summary = job.metadata.get("output_summary", {})
            output_type = output_summary.get("asset_type", "-")
            file_size = output_summary.get("file_size_bytes", "-")
            self.stdout.write(
                f"#{job.id} {job.state} | user={job.telegram_user.telegram_user_id} "
                f"| workflow={job.workflow_name} | seed={job.seed or '-'} "
                f"| priority={job.priority} | executor={job.requested_executor} "
                f"| output={output_type} | size={file_size} | failure={failure_type} "
                f"| created={job.created_at.isoformat()}"
            )

    def _show_job(self, job_id: int) -> None:
        job = self._get_job(job_id)
        lines = [
            f"id: {job.id}",
            f"state: {job.state}",
            f"telegram_user_id: {job.telegram_user.telegram_user_id}",
            f"workflow_name: {job.workflow_name}",
            f"prompt: {job.prompt}",
            f"seed: {job.seed}",
            f"priority: {job.priority}",
            f"requested_executor: {job.requested_executor}",
            f"input_media_id: {job.input_media_id}",
            f"output_media_id: {job.output_media_id or ''}",
            f"comfyui_prompt_id: {job.comfyui_prompt_id}",
            f"error_message: {job.error_message}",
            f"created_at: {job.created_at.isoformat()}",
            f"started_at: {job.started_at.isoformat() if job.started_at else ''}",
            f"completed_at: {job.completed_at.isoformat() if job.completed_at else ''}",
            "metadata:",
            json.dumps(job.metadata, indent=2, sort_keys=True),
        ]
        self.stdout.write("\n".join(lines))

    def _cancel_job(self, job_id: int) -> None:
        job = self._get_job(job_id)
        cancellation_error = self.job_service.get_cancellation_ineligibility_reason(job)
        if cancellation_error is not None:
            raise CommandError(cancellation_error)
        event_type, message = self.job_service.cancel_job(job)
        if event_type == "cancelled":
            message = f"Cancelled queued job #{job.id}."
        elif event_type == "cancellation_requested":
            message = f"Requested cancellation for running job #{job.id}."

        self.job_service.log_job_event(job, event_type, message)
        self.stdout.write(message)

    def _retry_job(self, job_id: int) -> None:
        job = self._get_job(job_id)
        retry_error = self.job_service.get_rerun_ineligibility_reason(job)
        if retry_error is not None:
            raise CommandError(retry_error)

        retry_job = self.job_service.create_rerun_job(job)
        retry_job.metadata["retried_from_job_id"] = job.id
        retry_job.save(update_fields=["metadata", "updated_at"])
        self.job_service.log_job_event(
            retry_job,
            "retried",
            f"Created retry job #{retry_job.id} from job #{job.id}.",
            {"source_job_id": job.id},
        )
        self.stdout.write(f"Created retry job #{retry_job.id} from job #{job.id}.")

    def _get_job(self, job_id: int) -> GenerationJob:
        try:
            return GenerationJob.objects.select_related("telegram_user").get(id=job_id)
        except GenerationJob.DoesNotExist as exc:
            raise CommandError(f"Job #{job_id} does not exist.") from exc
