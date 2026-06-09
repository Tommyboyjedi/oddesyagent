from django.core.files.base import ContentFile
from django.test import TestCase
from pathlib import Path

from apps.core.models import GenerationJob, MediaAsset, TelegramUser
from apps.core.services.job_service import JobService
from apps.core.services.oddesy_agent_service import OddesyAgentService


class OddesyAgentServiceTests(TestCase):
    def setUp(self) -> None:
        self.telegram_user = TelegramUser.objects.create(
            telegram_user_id=12345,
            username="tester",
            is_allowed=True,
        )
        self.other_user = TelegramUser.objects.create(
            telegram_user_id=67890,
            username="other",
            is_allowed=True,
        )
        self.input_media = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name="input.jpg",
            telegram_file_id="telegram-file-1",
            file=ContentFile(b"image-bytes", name="input.jpg"),
        )
        self.generated_media = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name="output.mp4",
            file=ContentFile(b"video-bytes", name="output.mp4"),
        )
        self.service = OddesyAgentService(job_service=JobService())

    def test_get_latest_input_media_returns_latest_owned_input(self) -> None:
        newer = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name="newer.jpg",
            file=ContentFile(b"newer-bytes", name="newer.jpg"),
        )

        media_asset = self.service.get_latest_input_media(self.telegram_user)

        self.assertEqual(media_asset.id, newer.id)

    def test_get_latest_generated_media_returns_latest_owned_output(self) -> None:
        newer = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="newer.png",
            file=ContentFile(b"newer-image", name="newer.png"),
        )

        media_asset = self.service.get_latest_generated_media(self.telegram_user)

        self.assertEqual(media_asset.id, newer.id)

    def test_get_latest_generated_media_skips_missing_cleaned_assets(self) -> None:
        newer = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="newer.png",
            file=ContentFile(b"newer-image", name="newer.png"),
            metadata={"cleanup": {"removed_from_library": True}},
        )
        Path(newer.file.path).unlink()

        media_asset = self.service.get_latest_generated_media(self.telegram_user)

        self.assertEqual(media_asset.id, self.generated_media.id)

    def test_create_job_from_existing_media_rejects_foreign_media(self) -> None:
        foreign_media = MediaAsset.objects.create(
            telegram_user=self.other_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name="foreign.jpg",
            file=ContentFile(b"foreign", name="foreign.jpg"),
        )

        with self.assertRaises(ValueError):
            self.service.create_job_from_existing_media(
                telegram_user=self.telegram_user,
                media_asset=foreign_media,
                workflow_name="workflow_a",
                prompt="make video",
            )

    def test_create_job_from_existing_media_creates_queued_job(self) -> None:
        job = self.service.create_job_from_existing_media(
            telegram_user=self.telegram_user,
            media_asset=self.input_media,
            workflow_name="i2v_wan_480p",
            prompt="make video",
            seed=42,
            metadata={"source": "service"},
        )

        self.assertEqual(job.state, GenerationJob.STATE_QUEUED)
        self.assertEqual(job.telegram_user_id, self.telegram_user.id)
        self.assertEqual(job.input_media_id, self.input_media.id)
        self.assertEqual(job.workflow_name, "i2v_wan_480p")
        self.assertEqual(job.seed, 42)
        self.assertEqual(job.metadata["source"], "service")
        self.assertEqual(job.priority, 100)
        self.assertEqual(job.requested_executor, GenerationJob.EXECUTOR_LOCAL_GPU)

    def test_create_job_from_existing_media_rejects_unknown_workflow(self) -> None:
        with self.assertRaises(ValueError):
            self.service.create_job_from_existing_media(
                telegram_user=self.telegram_user,
                media_asset=self.input_media,
                workflow_name="workflow_missing",
                prompt="make video",
            )

    def test_get_job_status_payload_returns_owned_job_payload(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="status prompt",
        )

        payload = self.service.get_job_status_payload(self.telegram_user, job.id)

        self.assertEqual(payload["id"], job.id)
        self.assertEqual(payload["workflow_name"], "workflow_b")
        self.assertEqual(payload["input_media_id"], self.input_media.id)
        self.assertEqual(payload["priority"], job.priority)
        self.assertEqual(payload["requested_executor"], job.requested_executor)

    def test_list_media_payloads_filters_by_asset_type(self) -> None:
        payloads = self.service.list_media_payloads(
            self.telegram_user,
            asset_types=[MediaAsset.TYPE_GENERATED_VIDEO],
            limit=10,
        )

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["id"], self.generated_media.id)
        self.assertEqual(payloads[0]["asset_type"], MediaAsset.TYPE_GENERATED_VIDEO)

    def test_get_generated_output_payload_returns_job_output(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            output_media=self.generated_media,
            workflow_name="workflow_c",
            prompt="output prompt",
        )

        payload = self.service.get_generated_output_payload(self.telegram_user, job.id)

        self.assertEqual(payload["id"], self.generated_media.id)
        self.assertEqual(payload["original_file_name"], "output.mp4")

    def test_get_generated_output_payload_returns_none_for_missing_cleaned_output(self) -> None:
        self.generated_media.metadata["cleanup"] = {"removed_from_library": True}
        self.generated_media.save(update_fields=["metadata"])
        Path(self.generated_media.file.path).unlink()
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            output_media=self.generated_media,
            workflow_name="workflow_c",
            prompt="output prompt",
        )

        payload = self.service.get_generated_output_payload(self.telegram_user, job.id)

        self.assertIsNone(payload)
