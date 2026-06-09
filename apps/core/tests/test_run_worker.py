from unittest.mock import Mock

from django.core.files.base import ContentFile
from django.test import TestCase

from apps.core.management.commands.run_worker import Command
from apps.core.models import GenerationJob, MediaAsset, TelegramUser


class RunWorkerCommandTests(TestCase):
    def setUp(self) -> None:
        self.command = Command()
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

    def test_claim_next_job_returns_none_when_cancellation_requested_job_exists(self) -> None:
        active_job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="running prompt",
        )
        active_job.mark_running()
        active_job.mark_cancellation_requested()
        GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="queued prompt",
        )

        claimed_job = self.command._claim_next_job()

        self.assertIsNone(claimed_job)

    def test_claim_next_job_prefers_highest_priority_job(self) -> None:
        low = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="low priority",
            priority=50,
        )
        high = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="high priority",
            priority=250,
        )

        claimed_job = self.command._claim_next_job()

        self.assertEqual(claimed_job.id, high.id)
        high.refresh_from_db()
        low.refresh_from_db()
        self.assertEqual(high.state, GenerationJob.STATE_RUNNING)
        self.assertEqual(low.state, GenerationJob.STATE_QUEUED)

    def test_process_job_marks_cancellation_requested_job_cancelled_before_submit(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="running prompt",
        )
        job.mark_running()
        job.mark_cancellation_requested()
        comfy_client = Mock()
        workflow_manager = Mock()

        self.command._process_job(
            job=job,
            bot=None,
            comfy_client=comfy_client,
            workflow_manager=workflow_manager,
            poll_seconds=1,
            timeout_seconds=1,
        )

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_CANCELLED)
        comfy_client.upload_input_image.assert_not_called()
        workflow_manager.render_workflow.assert_not_called()

    def test_process_job_records_output_summary_metadata(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="make video",
            seed=123,
        )
        job.mark_running()
        comfy_client = Mock()
        comfy_client.upload_input_image.return_value = "uploaded-input.png"
        comfy_client.submit_workflow.return_value = "prompt-123"
        comfy_client.wait_for_completion.return_value = {"outputs": {}}
        comfy_client.get_outputs_from_history.return_value = [
            {"filename": "clip.mp4", "subfolder": "videos", "type": "output", "duration": 6}
        ]
        comfy_client.download_output_file.return_value = b"video-bytes"
        workflow_manager = Mock()
        workflow_manager.render_workflow.return_value = {"1": {"inputs": {}}}

        self.command._process_job(
            job=job,
            bot=None,
            comfy_client=comfy_client,
            workflow_manager=workflow_manager,
            poll_seconds=1,
            timeout_seconds=1,
        )

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_COMPLETED)
        self.assertEqual(job.metadata["output_summary"]["asset_type"], MediaAsset.TYPE_GENERATED_VIDEO)
        self.assertEqual(job.metadata["output_summary"]["file_size_bytes"], len(b"video-bytes"))
        self.assertEqual(job.metadata["output_summary"]["duration_seconds"], 6.0)
        self.assertEqual(job.metadata["output_summary"]["comfyui_filename"], "clip.mp4")
        self.assertEqual(job.output_media.metadata["file_size_bytes"], len(b"video-bytes"))
        self.assertEqual(job.output_media.metadata["duration_seconds"], 6.0)
        self.assertEqual(job.output_media.metadata["comfyui_subfolder"], "videos")

    def test_process_job_records_structured_failure_metadata_for_missing_output(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="make video",
            seed=123,
        )
        job.mark_running()
        comfy_client = Mock()
        comfy_client.upload_input_image.return_value = "uploaded-input.png"
        comfy_client.submit_workflow.return_value = "prompt-123"
        comfy_client.wait_for_completion.return_value = {"outputs": {}}
        comfy_client.get_outputs_from_history.return_value = []
        workflow_manager = Mock()
        workflow_manager.render_workflow.return_value = {"1": {"inputs": {}}}

        self.command._process_job(
            job=job,
            bot=None,
            comfy_client=comfy_client,
            workflow_manager=workflow_manager,
            poll_seconds=1,
            timeout_seconds=1,
        )

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_FAILED)
        self.assertEqual(job.metadata["failure"]["failure_type"], "output_missing")
        self.assertTrue(job.metadata["failure"]["retry_safe"])

    def test_process_job_marks_running_job_cancelled_after_remote_completion_if_cancellation_requested(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="make video",
            seed=123,
        )
        job.mark_running()
        comfy_client = Mock()
        comfy_client.upload_input_image.return_value = "uploaded-input.png"
        comfy_client.submit_workflow.return_value = "prompt-123"

        def cancel_during_wait(*args, **kwargs):
            GenerationJob.objects.filter(id=job.id).update(state=GenerationJob.STATE_CANCELLATION_REQUESTED)
            return {"outputs": {}}

        comfy_client.wait_for_completion.side_effect = cancel_during_wait
        comfy_client.get_outputs_from_history.return_value = [
            {"filename": "clip.mp4", "subfolder": "videos", "type": "output", "duration": 6}
        ]
        comfy_client.download_output_file.return_value = b"video-bytes"
        workflow_manager = Mock()
        workflow_manager.render_workflow.return_value = {"1": {"inputs": {}}}

        self.command._process_job(
            job=job,
            bot=None,
            comfy_client=comfy_client,
            workflow_manager=workflow_manager,
            poll_seconds=1,
            timeout_seconds=1,
        )

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_CANCELLED)
        self.assertIsNotNone(job.output_media)
        self.assertEqual(job.metadata["cancellation_result"]["output_media_id"], job.output_media_id)
        self.assertTrue(job.metadata["cancellation_result"]["suppressed_delivery"])
