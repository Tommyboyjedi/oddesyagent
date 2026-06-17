from unittest.mock import AsyncMock, Mock

from django.core.files.base import ContentFile
from django.test import TestCase
from django.utils import timezone

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

    def _build_comfy_client(self) -> Mock:
        comfy_client = Mock()
        comfy_client.extract_execution_error_from_prompt_history.return_value = None
        return comfy_client

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
        comfy_client = self._build_comfy_client()
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
        workflow_manager.render_generation_workflow.assert_not_called()

    def test_process_job_records_output_summary_metadata(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="make video",
            seed=123,
        )
        job.mark_running()
        comfy_client = self._build_comfy_client()
        comfy_client.upload_input_image.return_value = "uploaded-input.png"
        comfy_client.submit_workflow.return_value = "prompt-123"
        comfy_client.wait_for_completion.return_value = {
            "outputs": {
                "12": {
                    "images": [
                        {"filename": "clip.mp4", "subfolder": "videos", "type": "output", "duration": 6}
                    ]
                }
            }
        }
        comfy_client.extract_outputs_from_prompt_history.return_value = [
            {"filename": "clip.mp4", "subfolder": "videos", "type": "output", "duration": 6}
        ]
        comfy_client.download_output_file.return_value = b"video-bytes"
        workflow_manager = Mock()
        workflow_manager.workflow_requires_input_media.return_value = True
        workflow_manager.render_generation_workflow.return_value = {"1": {"inputs": {}}}

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
        comfy_client = self._build_comfy_client()
        comfy_client.upload_input_image.return_value = "uploaded-input.png"
        comfy_client.submit_workflow.return_value = "prompt-123"
        comfy_client.wait_for_completion.return_value = {"outputs": {}}
        comfy_client.extract_outputs_from_prompt_history.return_value = []
        workflow_manager = Mock()
        workflow_manager.workflow_requires_input_media.return_value = True
        workflow_manager.render_generation_workflow.return_value = {"1": {"inputs": {}}}

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

    def test_process_job_records_comfyui_execution_error_from_history(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="make video",
            seed=123,
        )
        job.mark_running()
        comfy_client = self._build_comfy_client()
        comfy_client.upload_input_image.return_value = "uploaded-input.png"
        comfy_client.submit_workflow.return_value = "prompt-123"
        comfy_client.wait_for_completion.return_value = {
            "outputs": {},
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [
                    [
                        "execution_error",
                        {
                            "node_id": "172",
                            "node_type": "SamplerCustomAdvanced",
                            "exception_message": "Sizes of tensors must match",
                        },
                    ]
                ],
            },
        }
        comfy_client.extract_execution_error_from_prompt_history.return_value = (
            "Sizes of tensors must match (node 172:SamplerCustomAdvanced)"
        )
        comfy_client.extract_outputs_from_prompt_history.return_value = []
        workflow_manager = Mock()
        workflow_manager.workflow_requires_input_media.return_value = True
        workflow_manager.render_generation_workflow.return_value = {"1": {"inputs": {}}}

        self.command._process_job(
            job=job,
            bot=None,
            comfy_client=comfy_client,
            workflow_manager=workflow_manager,
            poll_seconds=1,
            timeout_seconds=1800,
        )

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_FAILED)
        self.assertEqual(job.metadata["failure"]["failure_type"], "comfyui_execution_error")
        self.assertIn("Sizes of tensors must match", job.error_message)

    def test_process_job_marks_running_job_cancelled_after_remote_completion_if_cancellation_requested(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="make video",
            seed=123,
        )
        job.mark_running()
        comfy_client = self._build_comfy_client()
        comfy_client.upload_input_image.return_value = "uploaded-input.png"
        comfy_client.submit_workflow.return_value = "prompt-123"

        def cancel_during_wait(*args, **kwargs):
            GenerationJob.objects.filter(id=job.id).update(state=GenerationJob.STATE_CANCELLATION_REQUESTED)
            return {
                "outputs": {
                    "12": {
                        "images": [
                            {"filename": "clip.mp4", "subfolder": "videos", "type": "output", "duration": 6}
                        ]
                    }
                }
            }

        comfy_client.wait_for_completion.side_effect = cancel_during_wait
        comfy_client.extract_outputs_from_prompt_history.return_value = [
            {"filename": "clip.mp4", "subfolder": "videos", "type": "output", "duration": 6}
        ]
        comfy_client.download_output_file.return_value = b"video-bytes"
        workflow_manager = Mock()
        workflow_manager.workflow_requires_input_media.return_value = True
        workflow_manager.render_generation_workflow.return_value = {"1": {"inputs": {}}}

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

    def test_process_job_supports_prompt_only_workflow_without_uploading_input(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=None,
            workflow_name="jugg_latent_cyberpony (1)",
            prompt="cyberpunk alley portrait",
            seed=123,
        )
        job.mark_running()
        comfy_client = self._build_comfy_client()
        comfy_client.submit_workflow.return_value = "prompt-123"
        comfy_client.wait_for_completion.return_value = {
            "outputs": {
                "12": {
                    "images": [
                        {"filename": "image.png", "subfolder": "images", "type": "output"}
                    ]
                }
            }
        }
        comfy_client.extract_outputs_from_prompt_history.return_value = [
            {"filename": "image.png", "subfolder": "images", "type": "output"}
        ]
        comfy_client.download_output_file.return_value = b"image-bytes"
        workflow_manager = Mock()
        workflow_manager.workflow_requires_input_media.return_value = False
        workflow_manager.render_generation_workflow.return_value = {"1": {"inputs": {}}}

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
        self.assertEqual(job.output_media.asset_type, MediaAsset.TYPE_GENERATED_IMAGE)
        comfy_client.upload_input_image.assert_not_called()

    def test_process_job_keeps_completed_state_when_result_delivery_fails(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=None,
            workflow_name="jugg_latent_cyberpony (1)",
            prompt="beach portrait",
            seed=123,
        )
        job.mark_running()
        comfy_client = self._build_comfy_client()
        comfy_client.submit_workflow.return_value = "prompt-123"
        comfy_client.wait_for_completion.return_value = {
            "outputs": {
                "12": {
                    "images": [
                        {"filename": "final.png", "subfolder": "", "type": "output"},
                        {"filename": "preview.png", "subfolder": "", "type": "temp"},
                    ]
                }
            }
        }
        comfy_client.extract_outputs_from_prompt_history.return_value = [
            {"filename": "final.png", "subfolder": "", "type": "output"},
            {"filename": "preview.png", "subfolder": "", "type": "temp"},
        ]
        comfy_client.download_output_file.return_value = b"image-bytes"
        workflow_manager = Mock()
        workflow_manager.workflow_requires_input_media.return_value = False
        workflow_manager.render_generation_workflow.return_value = {"1": {"inputs": {}}}
        bot = Mock()
        bot.send_document.side_effect = RuntimeError("Event loop is closed")

        self.command._process_job(
            job=job,
            bot=bot,
            comfy_client=comfy_client,
            workflow_manager=workflow_manager,
            poll_seconds=1,
            timeout_seconds=1,
        )

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_COMPLETED)
        self.assertEqual(job.output_media.original_file_name, "final.png")
        self.assertEqual(job.metadata["delivery"]["status"], "failed")
        self.assertEqual(job.metadata["delivery"]["stage"], "result")

    def test_process_job_swallow_failure_notice_delivery_errors(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=None,
            workflow_name="jugg_latent_cyberpony (1)",
            prompt="stormy landscape",
            seed=123,
        )
        job.mark_running()
        comfy_client = self._build_comfy_client()
        comfy_client.submit_workflow.side_effect = RuntimeError("submit failed")
        workflow_manager = Mock()
        workflow_manager.workflow_requires_input_media.return_value = False
        workflow_manager.render_generation_workflow.return_value = {"1": {"inputs": {}}}
        bot = Mock()
        bot.send_message.side_effect = RuntimeError("pool timeout")

        self.command._process_job(
            job=job,
            bot=bot,
            comfy_client=comfy_client,
            workflow_manager=workflow_manager,
            poll_seconds=1,
            timeout_seconds=1,
        )

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_FAILED)
        self.assertEqual(job.metadata["delivery"]["status"], "failed")
        self.assertEqual(job.metadata["delivery"]["stage"], "failure_notice")

    def test_send_result_includes_job_and_media_id_in_caption(self) -> None:
        asset = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name="clip.mp4",
            file=ContentFile(b"video-bytes", name="clip.mp4"),
        )
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            output_media=asset,
            workflow_name="workflow_a",
            prompt="make video",
            seed=123,
        )
        bot = Mock()
        bot.send_video = AsyncMock()

        self.command._send_result(bot, job, asset)

        self.assertEqual(
            bot.send_video.await_args.kwargs["caption"],
            f"Job #{job.id} completed. Media #{asset.id}.",
        )

    def test_recover_stale_running_jobs_marks_completed_when_history_has_outputs(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="make video",
            seed=123,
            comfyui_prompt_id="prompt-123",
        )
        job.mark_running()
        GenerationJob.objects.filter(id=job.id).update(updated_at=timezone.now() - timezone.timedelta(minutes=10))
        comfy_client = self._build_comfy_client()
        comfy_client.get_history.return_value = {
            "prompt-123": {
                "outputs": {
                    "12": {
                        "images": [
                            {"filename": "clip.mp4", "subfolder": "videos", "type": "output", "duration": 6}
                        ]
                    }
                },
                "status": {"completed": True, "status_str": "success"},
            }
        }
        comfy_client.extract_outputs_from_prompt_history.return_value = [
            {"filename": "clip.mp4", "subfolder": "videos", "type": "output", "duration": 6}
        ]
        comfy_client.download_output_file.return_value = b"video-bytes"

        self.command._recover_stale_running_jobs(comfy_client, stale_after_seconds=60)

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_COMPLETED)
        self.assertIsNotNone(job.output_media)
        self.assertEqual(job.metadata["recovery"]["status"], "completed_from_history")

    def test_process_job_sends_all_image_outputs_when_user_mode_is_all(self) -> None:
        self.telegram_user.image_output_mode = TelegramUser.IMAGE_OUTPUT_MODE_ALL
        self.telegram_user.save(update_fields=["image_output_mode"])
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=None,
            workflow_name="jugg_latent_cyberpony (1)",
            prompt="city skyline",
            seed=123,
        )
        job.mark_running()
        comfy_client = self._build_comfy_client()
        comfy_client.submit_workflow.return_value = "prompt-123"
        comfy_client.wait_for_completion.return_value = {
            "outputs": {
                "12": {
                    "images": [
                        {"filename": "final.png", "subfolder": "", "type": "output"},
                        {"filename": "preview.png", "subfolder": "", "type": "temp"},
                    ]
                }
            }
        }
        comfy_client.extract_outputs_from_prompt_history.return_value = [
            {"filename": "final.png", "subfolder": "", "type": "output"},
            {"filename": "preview.png", "subfolder": "", "type": "temp"},
        ]
        comfy_client.download_output_file.side_effect = [b"final-bytes", b"preview-bytes"]
        workflow_manager = Mock()
        workflow_manager.workflow_requires_input_media.return_value = False
        workflow_manager.render_generation_workflow.return_value = {"1": {"inputs": {}}}
        self.command._send_result_with_fresh_bot = AsyncMock(return_value=None)

        self.command._process_job(
            job=job,
            bot="token",
            comfy_client=comfy_client,
            workflow_manager=workflow_manager,
            poll_seconds=1,
            timeout_seconds=1,
        )

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_COMPLETED)
        self.assertEqual(len(job.metadata["additional_output_media_ids"]), 1)
        self.command._send_result_with_fresh_bot.assert_called_once()
        sent_extra_assets = self.command._send_result_with_fresh_bot.call_args.args[3]
        self.assertEqual(len(sent_extra_assets), 1)
        self.assertEqual(sent_extra_assets[0].original_file_name, "preview.png")
