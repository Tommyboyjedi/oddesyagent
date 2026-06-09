from django.core.files.base import ContentFile
from django.test import TestCase

from apps.core.models import GenerationJob, MediaAsset, TelegramUser
from apps.core.services.job_scheduler import JobSchedulerService


class JobSchedulerServiceTests(TestCase):
    def setUp(self) -> None:
        self.scheduler = JobSchedulerService()
        self.telegram_user = TelegramUser.objects.create(
            telegram_user_id=12345,
            username="tester",
            is_allowed=True,
        )
        self.input_media = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name="input.jpg",
            file=ContentFile(b"image", name="input.jpg"),
        )

    def test_build_generation_job_defaults_injects_scheduling_metadata(self) -> None:
        defaults = self.scheduler.build_generation_job_defaults(priority=200, metadata={"source": "telegram"})

        self.assertEqual(defaults["priority"], 200)
        self.assertEqual(defaults["requested_executor"], GenerationJob.EXECUTOR_LOCAL_GPU)
        self.assertEqual(defaults["metadata"]["scheduling"]["priority"], 200)
        self.assertEqual(defaults["metadata"]["source"], "telegram")

    def test_claim_next_generation_job_prefers_highest_priority(self) -> None:
        low = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="low",
            priority=50,
        )
        high = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="high",
            priority=200,
        )

        claimed = self.scheduler.claim_next_generation_job()

        self.assertEqual(claimed.id, high.id)
        high.refresh_from_db()
        low.refresh_from_db()
        self.assertEqual(high.state, GenerationJob.STATE_RUNNING)
        self.assertEqual(low.state, GenerationJob.STATE_QUEUED)

    def test_claim_next_generation_job_returns_none_when_local_gpu_busy(self) -> None:
        running = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="running",
        )
        running.mark_running()
        GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="queued",
            priority=200,
        )

        claimed = self.scheduler.claim_next_generation_job()

        self.assertIsNone(claimed)

    def test_snapshot_counts_executor_lanes(self) -> None:
        GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="queued local",
            requested_executor=GenerationJob.EXECUTOR_LOCAL_GPU,
        )
        cloud = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="queued cloud",
            requested_executor=GenerationJob.EXECUTOR_CLOUD,
        )
        cloud.mark_running()

        snapshot = self.scheduler.snapshot()

        self.assertEqual(snapshot.queued_local_gpu, 1)
        self.assertEqual(snapshot.running_local_gpu, 0)
        self.assertEqual(snapshot.queued_cloud, 0)
        self.assertEqual(snapshot.running_cloud, 1)
