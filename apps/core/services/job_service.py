from __future__ import annotations

from apps.core.models import AuditLog, GenerationJob, MediaAsset, TelegramUser
from apps.core.services.job_scheduler import JobSchedulerService


class JobService:
    def __init__(self, scheduler: JobSchedulerService | None = None) -> None:
        self.scheduler = scheduler or JobSchedulerService()

    def create_generation_job(
        self,
        telegram_user: TelegramUser,
        input_media: MediaAsset | None,
        workflow_name: str,
        prompt: str,
        seed: int = 0,
        priority: int | None = None,
        requested_executor: str = GenerationJob.EXECUTOR_LOCAL_GPU,
        metadata: dict | None = None,
    ) -> GenerationJob:
        scheduler_defaults = self.scheduler.build_generation_job_defaults(
            priority=priority,
            requested_executor=requested_executor,
            metadata=metadata,
        )
        return GenerationJob.objects.create(
            telegram_user=telegram_user,
            input_media=input_media,
            workflow_name=workflow_name,
            prompt=prompt,
            seed=seed,
            priority=scheduler_defaults["priority"],
            requested_executor=scheduler_defaults["requested_executor"],
            metadata=scheduler_defaults["metadata"],
        )

    def get_rerunnable_job(self, telegram_user: TelegramUser, args: list[str]) -> GenerationJob | None:
        if args:
            try:
                job_id = int(args[0])
            except ValueError:
                return None
            return telegram_user.generation_jobs.filter(
                id=job_id,
                state__in=[
                    GenerationJob.STATE_COMPLETED,
                    GenerationJob.STATE_FAILED,
                    GenerationJob.STATE_CANCELLED,
                ],
            ).first()
        return (
            telegram_user.generation_jobs.filter(state=GenerationJob.STATE_COMPLETED)
            .order_by("-created_at", "-id")
            .first()
        )

    def get_rerun_ineligibility_reason(self, job: GenerationJob) -> str | None:
        if job.state == GenerationJob.STATE_COMPLETED:
            return None
        if job.state == GenerationJob.STATE_CANCELLED:
            return None
        if job.state == GenerationJob.STATE_FAILED:
            failure = job.metadata.get("failure", {})
            if failure.get("retry_safe", False):
                return None
            failure_type = failure.get("failure_type", "unknown")
            return f"Job #{job.id} failed with non-retry-safe error type '{failure_type}'."
        return f"Job #{job.id} is not eligible for rerun."

    def create_rerun_job(self, source_job: GenerationJob) -> GenerationJob:
        rerun_metadata = {"rerun_of_job_id": source_job.id}
        if source_job.metadata.get("parsed_instruction"):
            rerun_metadata["parsed_instruction"] = source_job.metadata["parsed_instruction"]
        scheduler_defaults = self.scheduler.build_generation_job_defaults(
            priority=source_job.priority,
            requested_executor=source_job.requested_executor,
            metadata=rerun_metadata,
        )
        return GenerationJob.objects.create(
            telegram_user=source_job.telegram_user,
            input_media_id=source_job.input_media_id,
            workflow_name=source_job.workflow_name,
            prompt=source_job.prompt,
            seed=source_job.seed,
            priority=scheduler_defaults["priority"],
            requested_executor=scheduler_defaults["requested_executor"],
            metadata=scheduler_defaults["metadata"],
        )

    def log_job_event(
        self,
        job: GenerationJob,
        event_type: str,
        message: str,
        metadata: dict | None = None,
    ) -> AuditLog:
        return AuditLog.objects.create(
            event_type=event_type,
            telegram_user=job.telegram_user,
            generation_job=job,
            message=message,
            metadata=metadata or {"job_id": job.id},
        )

    def get_latest_cancellable_job(self, telegram_user: TelegramUser) -> GenerationJob | None:
        return (
            telegram_user.generation_jobs.filter(
                state__in=[
                    GenerationJob.STATE_QUEUED,
                    GenerationJob.STATE_RUNNING,
                    GenerationJob.STATE_CANCELLATION_REQUESTED,
                ]
            )
            .order_by("-created_at", "-id")
            .first()
        )

    def get_cancellation_ineligibility_reason(self, job: GenerationJob) -> str | None:
        if job.state in [
            GenerationJob.STATE_QUEUED,
            GenerationJob.STATE_RUNNING,
            GenerationJob.STATE_CANCELLATION_REQUESTED,
        ]:
            return None
        return f"Job #{job.id} is not queued or running."

    def cancel_job(self, job: GenerationJob) -> tuple[str, str]:
        if job.state == GenerationJob.STATE_QUEUED:
            job.mark_cancelled()
            return "cancelled", f"Cancelled job #{job.id}."

        if job.state in [GenerationJob.STATE_RUNNING, GenerationJob.STATE_CANCELLATION_REQUESTED]:
            job.mark_cancellation_requested()
            return "cancellation_requested", (
                f"Cancellation requested for job #{job.id}. The worker will stop it if possible."
            )

        raise ValueError(f"Job #{job.id} is not queued or running.")
