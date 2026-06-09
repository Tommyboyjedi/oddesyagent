from django.core.files.base import ContentFile
from django.test import TestCase

from apps.core.models import GenerationJob, MediaAsset, TelegramUser


class GenerationJobModelTests(TestCase):
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

    def test_state_methods_update_job(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="i2v_wan_480p",
            prompt="make video",
        )

        job.mark_running()
        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_RUNNING)
        self.assertIsNotNone(job.started_at)

        job.mark_cancellation_requested()
        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_CANCELLATION_REQUESTED)
        self.assertIn("cancellation_requested_at", job.metadata)

        output_media = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name="output.mp4",
            file=ContentFile(b"video-bytes", name="output.mp4"),
        )
        job.mark_completed(output_media)
        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_COMPLETED)
        self.assertEqual(job.output_media_id, output_media.id)
        self.assertEqual(job.error_message, "")

        job.mark_failed("boom")
        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_FAILED)
        self.assertEqual(job.error_message, "boom")

        job.mark_cancelled()
        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_CANCELLED)
