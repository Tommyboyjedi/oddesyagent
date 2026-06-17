from django.core.files.base import ContentFile
from django.test import TestCase

from apps.core.models import AuditLog, GenerationJob, MediaAsset, TelegramUser
from apps.core.services.job_service import JobService


class JobServiceTests(TestCase):
    def setUp(self) -> None:
        self.job_service = JobService()
        self.telegram_user = TelegramUser.objects.create(
            telegram_user_id=12345,
            username="tester",
            is_allowed=True,
        )
        self.input_media = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name="input.jpg",
            telegram_file_id="telegram-file-1",
            file=ContentFile(b"image-bytes", name="input.jpg"),
        )

    def test_create_generation_job(self) -> None:
        job = self.job_service.create_generation_job(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="make video",
            seed=77,
            metadata={"source": "telegram"},
        )

        self.assertEqual(job.telegram_user_id, self.telegram_user.id)
        self.assertEqual(job.input_media_id, self.input_media.id)
        self.assertEqual(job.workflow_name, "workflow_a")
        self.assertEqual(job.prompt, "make video")
        self.assertEqual(job.seed, 77)
        self.assertEqual(job.metadata["source"], "telegram")
        self.assertEqual(job.priority, 100)
        self.assertEqual(job.requested_executor, GenerationJob.EXECUTOR_LOCAL_GPU)
        self.assertEqual(job.metadata["scheduling"]["priority"], 100)

    def test_create_generation_job_allows_prompt_only_jobs(self) -> None:
        job = self.job_service.create_generation_job(
            telegram_user=self.telegram_user,
            input_media=None,
            workflow_name="jugg_latent_cyberpony (1)",
            prompt="cyberpunk street portrait",
        )

        self.assertIsNone(job.input_media_id)
        self.assertEqual(job.workflow_name, "jugg_latent_cyberpony (1)")

    def test_get_rerunnable_job_defaults_to_latest_completed(self) -> None:
        older = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="older",
        )
        older.mark_completed()
        newer = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="newer",
        )
        newer.mark_completed()

        job = self.job_service.get_rerunnable_job(self.telegram_user, [])

        self.assertEqual(job.id, newer.id)

    def test_get_rerun_ineligibility_reason_rejects_non_retry_safe_failure(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_c",
            prompt="bad prompt",
            metadata={
                "failure": {
                    "failure_type": "unknown",
                    "retry_safe": False,
                }
            },
        )
        job.mark_failed("boom")

        reason = self.job_service.get_rerun_ineligibility_reason(job)

        self.assertEqual(reason, f"Job #{job.id} failed with non-retry-safe error type 'unknown'.")

    def test_create_rerun_job_copies_source_job(self) -> None:
        source_job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_d",
            prompt="retry prompt",
            seed=88,
        )
        source_job.mark_completed()

        rerun_job = self.job_service.create_rerun_job(source_job)

        self.assertEqual(rerun_job.state, GenerationJob.STATE_QUEUED)
        self.assertEqual(rerun_job.telegram_user_id, source_job.telegram_user_id)
        self.assertEqual(rerun_job.input_media_id, source_job.input_media_id)
        self.assertEqual(rerun_job.workflow_name, source_job.workflow_name)
        self.assertEqual(rerun_job.prompt, source_job.prompt)
        self.assertEqual(rerun_job.seed, source_job.seed)
        self.assertEqual(rerun_job.metadata["rerun_of_job_id"], source_job.id)
        self.assertEqual(rerun_job.priority, source_job.priority)
        self.assertEqual(rerun_job.requested_executor, source_job.requested_executor)

    def test_get_latest_cancellable_job_returns_latest_active_job(self) -> None:
        older = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_e",
            prompt="older active",
        )
        newer = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_f",
            prompt="newer active",
        )
        newer.mark_running()

        job = self.job_service.get_latest_cancellable_job(self.telegram_user)

        self.assertEqual(job.id, newer.id)

    def test_cancel_job_cancels_queued_job(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_g",
            prompt="queued",
        )

        event_type, message = self.job_service.cancel_job(job)

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_CANCELLED)
        self.assertEqual(event_type, "cancelled")
        self.assertEqual(message, f"Cancelled job #{job.id}.")

    def test_cancel_job_requests_cancellation_for_running_job(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_h",
            prompt="running",
        )
        job.mark_running()

        event_type, message = self.job_service.cancel_job(job)

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_CANCELLATION_REQUESTED)
        self.assertEqual(event_type, "cancellation_requested")
        self.assertEqual(message, f"Cancellation requested for job #{job.id}. The worker will stop it if possible.")

    def test_log_job_event_creates_audit_log(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_i",
            prompt="log event",
        )

        audit_log = self.job_service.log_job_event(
            job,
            "job_created",
            "queued",
            {"job_id": job.id, "input_media_id": self.input_media.id},
        )

        self.assertEqual(AuditLog.objects.count(), 1)
        self.assertEqual(audit_log.telegram_user_id, self.telegram_user.id)
        self.assertEqual(audit_log.generation_job_id, job.id)
        self.assertEqual(audit_log.event_type, "job_created")
        self.assertEqual(audit_log.message, "queued")
        self.assertEqual(audit_log.metadata["input_media_id"], self.input_media.id)
