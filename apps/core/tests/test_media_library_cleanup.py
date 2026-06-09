from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import GenerationJob, MediaAsset, TelegramUser
from apps.core.services.media_library_cleanup import MediaLibraryCleanupService


class MediaLibraryCleanupServiceTests(TestCase):
    def setUp(self) -> None:
        self.service = MediaLibraryCleanupService()
        self.telegram_user = TelegramUser.objects.create(
            telegram_user_id=4444,
            username="cleanup",
            is_allowed=True,
        )

    def test_execute_deletes_generated_media_and_records_cleanup_metadata(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with override_settings(MEDIA_ROOT=temp_dir, ODDESY_SAFE_ROOTS=[temp_dir]):
                input_media = MediaAsset.objects.create(
                    telegram_user=self.telegram_user,
                    asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
                    original_file_name="input.jpg",
                    file=ContentFile(b"image", name="input.jpg"),
                )
                generated_media = MediaAsset.objects.create(
                    telegram_user=self.telegram_user,
                    asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
                    original_file_name="output.mp4",
                    file=ContentFile(b"video", name="output.mp4"),
                )
                job = GenerationJob.objects.create(
                    telegram_user=self.telegram_user,
                    input_media=input_media,
                    output_media=generated_media,
                    workflow_name="workflow_a",
                    prompt="prompt",
                )
                job.mark_completed(generated_media)

                payload = self.service.execute(limit=10)
                generated_media.refresh_from_db()
                job.refresh_from_db()

                self.assertEqual(payload["deleted_count"], 1)
                self.assertEqual(payload["deleted_asset_ids"], [generated_media.id])
                self.assertEqual(payload["freed_bytes"], len(b"video"))
                self.assertFalse(Path(generated_media.file.path).exists())
                self.assertEqual(generated_media.metadata["cleanup"]["output_job_ids"], [job.id])
                self.assertTrue(generated_media.metadata["cleanup"]["file_existed"])
                self.assertTrue(generated_media.metadata["cleanup"]["removed_from_library"])
                self.assertIsNone(job.output_media)

    def test_execute_marks_missing_files_without_counting_them_as_deleted(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with override_settings(MEDIA_ROOT=temp_dir, ODDESY_SAFE_ROOTS=[temp_dir]):
                generated_media = MediaAsset.objects.create(
                    telegram_user=self.telegram_user,
                    asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
                    original_file_name="missing.mp4",
                    file=ContentFile(b"video", name="missing.mp4"),
                )
                Path(generated_media.file.path).unlink()

                payload = self.service.execute(limit=10, include_missing_files=True)
                generated_media.refresh_from_db()

                self.assertEqual(payload["deleted_count"], 0)
                self.assertEqual(payload["skipped_asset_ids"], [generated_media.id])
                self.assertFalse(generated_media.metadata["cleanup"]["file_existed"])

    def test_execute_respects_age_filter(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with override_settings(MEDIA_ROOT=temp_dir, ODDESY_SAFE_ROOTS=[temp_dir]):
                generated_media = MediaAsset.objects.create(
                    telegram_user=self.telegram_user,
                    asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
                    original_file_name="old.mp4",
                    file=ContentFile(b"video", name="old.mp4"),
                )
                generated_media.created_at = timezone.now() - timedelta(days=3)
                generated_media.save(update_fields=["created_at"])

                payload = self.service.execute(limit=10, older_than_days=1)

                self.assertEqual(payload["deleted_count"], 1)
