from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import GenerationJob, MediaAsset, TelegramUser
from apps.core.services.media_library_report import MediaLibraryReportService


class MediaLibraryReportServiceTests(TestCase):
    def setUp(self) -> None:
        self.service = MediaLibraryReportService()
        self.telegram_user = TelegramUser.objects.create(
            telegram_user_id=12345,
            username="tester",
            is_allowed=True,
        )

    def test_generated_media_cleanup_report_lists_generated_assets_and_job_links(self) -> None:
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

                payload = self.service.generated_media_cleanup_report(limit=10)

            self.assertEqual(payload["candidate_count"], 1)
            candidate = payload["candidates"][0]
            self.assertEqual(candidate["media_asset_id"], generated_media.id)
            self.assertEqual(candidate["output_job_ids"], [job.id])
            self.assertTrue(candidate["file_exists"])
            self.assertEqual(candidate["size_bytes"], len(b"video"))

    def test_generated_media_cleanup_report_includes_missing_files_when_enabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with override_settings(MEDIA_ROOT=temp_dir, ODDESY_SAFE_ROOTS=[temp_dir]):
                generated_media = MediaAsset.objects.create(
                    telegram_user=self.telegram_user,
                    asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
                    original_file_name="missing.mp4",
                    file=ContentFile(b"video", name="missing.mp4"),
                )
                file_path = Path(generated_media.file.path)
                file_path.unlink()

                payload = self.service.generated_media_cleanup_report(limit=10, include_missing_files=True)

            self.assertEqual(payload["candidate_count"], 1)
            self.assertFalse(payload["candidates"][0]["file_exists"])

    def test_generated_media_cleanup_report_filters_by_age(self) -> None:
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

                payload = self.service.generated_media_cleanup_report(limit=10, older_than_days=1)

            self.assertEqual(payload["candidate_count"], 1)
