from io import StringIO

from django.core.files.base import ContentFile
from django.core.management import call_command, CommandError
from django.test import TestCase

from apps.core.models import GenerationJob, MediaAsset, TelegramUser


class InspectJobsCommandTests(TestCase):
    def setUp(self) -> None:
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

    def test_lists_recent_jobs(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="prompt a",
            seed=10,
            metadata={
                "output_summary": {
                    "asset_type": MediaAsset.TYPE_GENERATED_VIDEO,
                    "file_size_bytes": 2048,
                },
                "failure": {
                    "failure_type": "unknown",
                },
            },
        )
        buffer = StringIO()

        call_command("inspect_jobs", stdout=buffer)

        output = buffer.getvalue()
        self.assertIn(
            f"#{job.id} queued | user=12345 | workflow=workflow_a | seed=10 | priority=100 | executor=local_gpu",
            output,
        )
        self.assertIn("output=generated_video | size=2048 | failure=unknown", output)

    def test_shows_job_detail(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="prompt a",
            seed=10,
            metadata={"key": "value"},
        )
        buffer = StringIO()

        call_command("inspect_jobs", job_id=job.id, stdout=buffer)

        output = buffer.getvalue()
        self.assertIn(f"id: {job.id}", output)
        self.assertIn("priority: 100", output)
        self.assertIn("requested_executor: local_gpu", output)
        self.assertIn("metadata:", output)
        self.assertIn('"key": "value"', output)

    def test_cancel_queued_job_marks_cancelled(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="prompt a",
        )
        buffer = StringIO()

        call_command("inspect_jobs", cancel=job.id, stdout=buffer)

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_CANCELLED)
        self.assertIn(f"Cancelled queued job #{job.id}.", buffer.getvalue())

    def test_cancel_running_job_marks_cancellation_requested(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="prompt a",
        )
        job.mark_running()
        buffer = StringIO()

        call_command("inspect_jobs", cancel=job.id, stdout=buffer)

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_CANCELLATION_REQUESTED)
        self.assertIn("cancellation_requested_at", job.metadata)
        self.assertIn(f"Requested cancellation for running job #{job.id}.", buffer.getvalue())

    def test_retry_terminal_job_creates_new_queued_job(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="prompt a",
            seed=77,
            metadata={
                "failure": {
                    "failure_type": "timeout",
                    "retry_safe": True,
                }
            },
        )
        job.mark_failed("boom")
        buffer = StringIO()

        call_command("inspect_jobs", retry=job.id, stdout=buffer)

        retry_job = GenerationJob.objects.exclude(id=job.id).get()
        self.assertEqual(retry_job.state, GenerationJob.STATE_QUEUED)
        self.assertEqual(retry_job.input_media_id, job.input_media_id)
        self.assertEqual(retry_job.workflow_name, job.workflow_name)
        self.assertEqual(retry_job.prompt, job.prompt)
        self.assertEqual(retry_job.seed, job.seed)
        self.assertEqual(retry_job.metadata["retried_from_job_id"], job.id)
        self.assertIn(f"Created retry job #{retry_job.id} from job #{job.id}.", buffer.getvalue())

    def test_retry_rejects_non_retry_safe_failed_job(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="prompt b",
            seed=78,
            metadata={
                "failure": {
                    "failure_type": "unknown",
                    "retry_safe": False,
                }
            },
        )
        job.mark_failed("boom")

        with self.assertRaises(CommandError):
            call_command("inspect_jobs", retry=job.id)
