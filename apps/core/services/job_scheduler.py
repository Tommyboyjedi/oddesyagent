from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from apps.core.models import AuditLog, GenerationJob


@dataclass(frozen=True)
class SchedulerSnapshot:
    queued_local_gpu: int
    running_local_gpu: int
    queued_cloud: int
    running_cloud: int


class JobSchedulerService:
    DEFAULT_PRIORITY = 100
    HIGH_PRIORITY = 200
    LOW_PRIORITY = 50

    def build_generation_job_defaults(
        self,
        *,
        priority: int | None = None,
        requested_executor: str = GenerationJob.EXECUTOR_LOCAL_GPU,
        metadata: dict | None = None,
    ) -> dict:
        resolved_priority = priority if priority is not None else self.DEFAULT_PRIORITY
        payload = dict(metadata or {})
        payload.setdefault("scheduling", {})
        payload["scheduling"].update(
            {
                "priority": resolved_priority,
                "requested_executor": requested_executor,
            }
        )
        return {
            "priority": resolved_priority,
            "requested_executor": requested_executor,
            "metadata": payload,
        }

    def claim_next_generation_job(self) -> GenerationJob | None:
        with transaction.atomic():
            local_gpu_running = (
                GenerationJob.objects.select_for_update()
                .filter(
                    requested_executor=GenerationJob.EXECUTOR_LOCAL_GPU,
                    state__in=[
                        GenerationJob.STATE_RUNNING,
                        GenerationJob.STATE_CANCELLATION_REQUESTED,
                    ],
                )
                .exists()
            )
            if local_gpu_running:
                return None

            job = (
                GenerationJob.objects.select_for_update()
                .filter(
                    state=GenerationJob.STATE_QUEUED,
                    requested_executor=GenerationJob.EXECUTOR_LOCAL_GPU,
                )
                .order_by("-priority", "created_at", "id")
                .first()
            )
            if job is None:
                return None

            job.mark_running()
            self._log_event(
                job,
                "job_transition",
                "running",
                {
                    "job_id": job.id,
                    "priority": job.priority,
                    "requested_executor": job.requested_executor,
                },
            )
            return job

    def snapshot(self) -> SchedulerSnapshot:
        return SchedulerSnapshot(
            queued_local_gpu=self._count_jobs(GenerationJob.EXECUTOR_LOCAL_GPU, GenerationJob.STATE_QUEUED),
            running_local_gpu=self._count_jobs(
                GenerationJob.EXECUTOR_LOCAL_GPU,
                GenerationJob.STATE_RUNNING,
                GenerationJob.STATE_CANCELLATION_REQUESTED,
            ),
            queued_cloud=self._count_jobs(GenerationJob.EXECUTOR_CLOUD, GenerationJob.STATE_QUEUED),
            running_cloud=self._count_jobs(
                GenerationJob.EXECUTOR_CLOUD,
                GenerationJob.STATE_RUNNING,
                GenerationJob.STATE_CANCELLATION_REQUESTED,
            ),
        )

    def _count_jobs(self, executor: str, *states: str) -> int:
        return GenerationJob.objects.filter(requested_executor=executor, state__in=states).count()

    def _log_event(self, job: GenerationJob, event_type: str, message: str, metadata: dict) -> None:
        AuditLog.objects.create(
            event_type=event_type,
            telegram_user=job.telegram_user,
            generation_job=job,
            message=message,
            metadata=metadata,
        )
