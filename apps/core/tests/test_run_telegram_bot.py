from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from django.core.files.base import ContentFile
from django.test import TransactionTestCase, override_settings

from apps.core.management.commands.run_telegram_bot import Command
from apps.core.models import GenerationJob, MediaAsset, TelegramUser
from apps.core.services.instruction_parser import ParsedIntent


@override_settings(
    TELEGRAM_ALLOWED_USER_IDS=[12345],
    WORKFLOWS_DIR=Path(__file__).resolve().parents[3] / "workflows",
)
class RunTelegramBotCommandTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._temp_media_root = tempfile.mkdtemp(prefix="oddesyagent-test-media-")
        cls._media_override = override_settings(MEDIA_ROOT=cls._temp_media_root)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._media_override.disable()
        shutil.rmtree(cls._temp_media_root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self) -> None:
        self.command = Command()
        self.command.get_or_reject_user = AsyncMock()
        self.command.log_event = AsyncMock()
        self.command.instruction_parser.parse_text = lambda text: ParsedIntent(
            action="create_job",
            workflow_name="i2v_wan_480p",
            prompt="make video",
            metadata={
                "parser": "fallback",
                "parsed_instruction": {
                    "workflow": "i2v_wan_480p",
                    "prompt": "make video",
                    "seed": 0,
                    "duration": None,
                    "motion": None,
                    "raw_text": text,
                },
            },
        )
        self.telegram_user = TelegramUser.objects.create(
            telegram_user_id=12345,
            username="tester",
            is_allowed=True,
        )
        self.command.get_or_reject_user.return_value = self.telegram_user
        self.command.oddesy_agent_service.workflow_requires_input_media = lambda workflow_name: workflow_name == "i2v_wan_480p"
        self.input_media = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name="input.jpg",
            telegram_file_id="telegram-file-1",
            file=ContentFile(b"image-bytes", name="input.jpg"),
        )

    def _build_update(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        message.message_id = 999
        return SimpleNamespace(
            effective_user=SimpleNamespace(
                id=self.telegram_user.telegram_user_id,
                username=self.telegram_user.username,
                first_name="Test",
                last_name="User",
            ),
            message=message,
            effective_chat=SimpleNamespace(id=self.telegram_user.telegram_user_id),
        )

    def _build_context(self, args=None):
        return SimpleNamespace(
            args=args or [],
            bot=SimpleNamespace(
                get_file=AsyncMock(),
                send_audio=AsyncMock(),
                send_chat_action=AsyncMock(),
                send_video=AsyncMock(),
                send_document=AsyncMock(),
            ),
        )

    def _build_callback_update(self, data: str):
        callback_query = SimpleNamespace(
            data=data,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
            from_user=SimpleNamespace(
                id=self.telegram_user.telegram_user_id,
                username=self.telegram_user.username,
                first_name="Test",
                last_name="User",
            ),
        )
        return SimpleNamespace(
            effective_user=callback_query.from_user,
            callback_query=callback_query,
            effective_chat=SimpleNamespace(id=self.telegram_user.telegram_user_id),
        )

    def test_queue_command_lists_active_jobs(self) -> None:
        queued = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="queued prompt",
            seed=11,
        )
        running = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="running prompt",
            seed=22,
        )
        running.mark_running()

        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.queue_command(update, context))

        reply = update.message.reply_text.await_args.args[0]
        self.assertIn(f"#{queued.id} queued | workflow_a | seed=11 | priority=100", reply)
        self.assertIn(f"#{running.id} running | workflow_b | seed=22 | priority=100", reply)

    def test_history_command_lists_recent_terminal_jobs(self) -> None:
        completed = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="finished prompt",
            seed=33,
            metadata={
                "output_summary": {
                    "asset_type": MediaAsset.TYPE_GENERATED_VIDEO,
                    "file_size_bytes": 4096,
                    "duration_seconds": 6.0,
                }
            },
        )
        completed.mark_completed()
        failed = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="failed prompt",
            seed=44,
            metadata={
                "failure": {
                    "failure_type": "output_missing",
                    "retry_safe": True,
                }
            },
        )
        failed.mark_failed("boom")

        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.history_command(update, context))

        reply = update.message.reply_text.await_args.args[0]
        self.assertIn(
            f"#{completed.id} completed | workflow_a | seed=33 | priority=100 | output=generated_video | size=4096 | duration=6.0",
            reply,
        )
        self.assertIn(
            f"#{failed.id} failed | workflow_b | seed=44 | priority=100 | failure=output_missing | retry_safe=True",
            reply,
        )

    def test_status_command_shows_output_summary(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="finished prompt for video generation",
            seed=66,
            metadata={
                "output_summary": {
                    "asset_type": MediaAsset.TYPE_GENERATED_VIDEO,
                    "file_size_bytes": 8192,
                    "duration_seconds": 5.5,
                }
            },
        )
        job.mark_completed()
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.status_command(update, context))

        reply = update.message.reply_text.await_args.args[0]
        self.assertIn(f"Job #{job.id}: completed", reply)
        self.assertIn("Workflow: workflow_a", reply)
        self.assertIn("Seed: 66", reply)
        self.assertIn("Output: generated_video | size=8192 | duration=5.5", reply)

    def test_status_command_shows_failure_summary(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="failed prompt",
            seed=77,
            metadata={
                "failure": {
                    "failure_type": "timeout",
                    "retry_safe": True,
                }
            },
        )
        job.mark_failed("Timed out waiting for ComfyUI prompt abc")
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.status_command(update, context))

        reply = update.message.reply_text.await_args.args[0]
        self.assertIn(f"Job #{job.id}: failed", reply)
        self.assertIn("Failure: timeout | retry_safe=True", reply)

    def test_rerun_command_queues_copy_of_completed_job(self) -> None:
        completed = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="finished prompt",
            seed=55,
        )
        completed.mark_completed()

        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.rerun_command(update, context))

        rerun_job = GenerationJob.objects.exclude(id=completed.id).get()
        self.assertEqual(rerun_job.state, GenerationJob.STATE_QUEUED)
        self.assertEqual(rerun_job.input_media_id, completed.input_media_id)
        self.assertEqual(rerun_job.workflow_name, completed.workflow_name)
        self.assertEqual(rerun_job.prompt, completed.prompt)
        self.assertEqual(rerun_job.seed, completed.seed)
        self.assertEqual(rerun_job.metadata["rerun_of_job_id"], completed.id)
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            f"Queued rerun job #{rerun_job.id} from job #{completed.id}.",
        )

    def test_rerun_command_allows_retry_safe_failed_job_by_id(self) -> None:
        failed = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="failed prompt",
            seed=91,
            metadata={
                "failure": {
                    "failure_type": "timeout",
                    "retry_safe": True,
                }
            },
        )
        failed.mark_failed("Timed out waiting for ComfyUI prompt abc")
        update = self._build_update()
        context = self._build_context(args=[str(failed.id)])

        self.async_run(self.command.rerun_command(update, context))

        rerun_job = GenerationJob.objects.exclude(id=failed.id).get()
        self.assertEqual(rerun_job.state, GenerationJob.STATE_QUEUED)
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            f"Queued rerun job #{rerun_job.id} from job #{failed.id}. Retry-safe failure: timeout.",
        )

    def test_rerun_command_rejects_non_retry_safe_failed_job(self) -> None:
        failed = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_c",
            prompt="bad prompt",
            seed=92,
            metadata={
                "failure": {
                    "failure_type": "unknown",
                    "retry_safe": False,
                }
            },
        )
        failed.mark_failed("Unknown failure")
        update = self._build_update()
        context = self._build_context(args=[str(failed.id)])

        self.async_run(self.command.rerun_command(update, context))

        self.assertEqual(GenerationJob.objects.exclude(id=failed.id).count(), 0)
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            f"Job #{failed.id} failed with non-retry-safe error type 'unknown'.",
        )

    def test_cancel_command_cancels_queued_job(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="queued prompt",
        )
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.cancel_command(update, context))

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_CANCELLED)
        self.assertEqual(update.message.reply_text.await_args.args[0], f"Cancelled job #{job.id}.")

    def test_cancel_command_requests_cancellation_for_running_job(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="running prompt",
        )
        job.mark_running()
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.cancel_command(update, context))

        job.refresh_from_db()
        self.assertEqual(job.state, GenerationJob.STATE_CANCELLATION_REQUESTED)
        self.assertIn("cancellation_requested_at", job.metadata)
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            f"Cancellation requested for job #{job.id}. The worker will stop it if possible.",
        )

    def test_text_message_queues_job_from_parsed_intent(self) -> None:
        update = self._build_update()
        update.message.text = "make video"
        context = self._build_context()

        self.async_run(self.command.text_message(update, context))

        job = GenerationJob.objects.latest("id")
        self.assertEqual(job.state, GenerationJob.STATE_QUEUED)
        self.assertEqual(job.prompt, "make video")
        self.assertEqual(job.workflow_name, "i2v_wan_480p")
        self.assertEqual(job.metadata["parsed_instruction"]["raw_text"], "make video")
        self.assertEqual(update.message.reply_text.await_args.args[0], f"Queued job #{job.id}.")

    def test_text_message_queues_multiple_video_jobs_when_batch_size_is_greater_than_one(self) -> None:
        self.command.oddesy_agent_service.get_generation_batch_count = lambda user: 2
        update = self._build_update()
        update.message.text = "make video"
        context = self._build_context()

        self.async_run(self.command.text_message(update, context))

        jobs = list(GenerationJob.objects.order_by("id"))
        self.assertEqual(len(jobs), 2)
        self.assertEqual([job.metadata["batch"]["index"] for job in jobs], [1, 2])
        self.assertEqual(jobs[0].metadata["batch"]["mode"], "input_media")
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            f"Queued 2 jobs: #{jobs[0].id}, #{jobs[1].id}.",
        )

    def test_photo_message_sets_pending_video_media(self) -> None:
        update = self._build_update()
        update.message.photo = [SimpleNamespace(file_id="telegram-photo-file-id", file_unique_id="photo-unique-id")]
        context = self._build_context()
        context.bot.get_file.return_value = SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"image-bytes"))
        )

        self.async_run(self.command.photo_message(update, context))

        self.telegram_user.refresh_from_db()
        asset = MediaAsset.objects.latest("id")
        self.assertEqual(asset.asset_type, MediaAsset.TYPE_INCOMING_IMAGE)
        self.assertEqual(self.telegram_user.pending_video_media_asset_id, asset.id)
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "Image saved. Your next plain-text prompt will use it for video, or use /video <prompt>. "
            "Image-to-image stays explicit via /referencephotoimageset.",
        )

    def test_photo_message_routes_into_active_imageswap_target_step(self) -> None:
        self.telegram_user.imageswap_draft = {"_wizard_step": "target_media_asset_id"}
        self.telegram_user.save(update_fields=["imageswap_draft"])
        update = self._build_update()
        update.message.photo = [SimpleNamespace(file_id="telegram-photo-file-id", file_unique_id="photo-unique-id")]
        context = self._build_context()
        context.bot.get_file.return_value = SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"image-bytes"))
        )

        self.async_run(self.command.photo_message(update, context))

        self.telegram_user.refresh_from_db()
        asset = MediaAsset.objects.latest("id")
        self.assertEqual(self.telegram_user.imageswap_draft["target_media_asset_id"], asset.id)
        self.assertEqual(self.telegram_user.imageswap_draft["_wizard_step"], "confirm")
        self.assertIsNone(self.telegram_user.pending_video_media_asset_id)
        self.assertIn("Referance Photo (image)", update.message.reply_text.await_args.args[0])

    def test_imageswap_command_starts_guided_flow_with_defaults(self) -> None:
        self.telegram_user.imageswap_draft = {}
        self.telegram_user.save(update_fields=["imageswap_draft"])
        self.command.oddesy_agent_service.get_imageswap_defaults = lambda user: {
            "workflow_name": "Flux Swap-Anything (Sam3.1)",
            "sam_prompt_text": "hairline and jaw",
            "positive_prompt": "portrait lighting",
        }
        self.command.oddesy_agent_service.get_active_text_workflow = lambda user: "Flux Swap-Anything (Sam3.1)"
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.imageswap_command(update, context))

        self.telegram_user.refresh_from_db()
        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("Image swap setup", reply)
        self.assertIn("sam3.1 prompt section", reply.lower())
        self.assertEqual(self.telegram_user.imageswap_draft["positive_prompt"], "portrait lighting")
        self.assertEqual(self.telegram_user.imageswap_draft["sam_prompt_text"], "hairline and jaw")
        self.assertEqual(self.telegram_user.imageswap_draft["_wizard_step"], "sam_prompt_text")

    def test_imageswap_confirm_saves_draft_as_defaults(self) -> None:
        generated_image = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="generated.png",
            file=ContentFile(b"generated-image", name="generated.png"),
        )
        self.telegram_user.imageswap_draft = {
            "workflow_name": "Flux Swap-Anything (Sam3.1)",
            "sam_prompt_text": "hat brim",
            "positive_prompt": "fashion studio",
            "swap_text_prompt": "velvet coat",
            "swap_reference_media_asset_id": generated_image.id,
            "target_media_asset_id": self.input_media.id,
            "_wizard_step": "confirm",
        }
        self.telegram_user.save(update_fields=["imageswap_draft"])
        self.command.oddesy_agent_service.build_imageswap_request = lambda user: {
            "job_payload": {
                "workflow_name": "Flux Swap-Anything (Sam3.1)",
                "media_asset": self.input_media,
                "prompt": "fashion studio",
                "metadata": {"imageswap": {"sam_prompt_text": "hat brim"}},
            }
        }
        self.command.oddesy_agent_service.create_job_from_existing_media = lambda **kwargs: GenerationJob.objects.create(
            telegram_user=kwargs["telegram_user"],
            input_media=kwargs["media_asset"],
            workflow_name=kwargs["workflow_name"],
            prompt=kwargs["prompt"],
            metadata=kwargs["metadata"],
        )

        self.async_run(self.command._confirm_imageswap(self._build_update(), self._build_context(), self.telegram_user))

        self.telegram_user.refresh_from_db()
        self.assertEqual(self.telegram_user.imageswap_defaults["sam_prompt_text"], "hat brim")
        self.assertEqual(self.telegram_user.imageswap_defaults["positive_prompt"], "fashion studio")
        self.assertEqual(self.telegram_user.imageswap_defaults["swap_text_prompt"], "velvet coat")
        self.assertEqual(self.telegram_user.imageswap_draft, {})

    def test_imageswap_callback_can_jump_to_edit_specific_field(self) -> None:
        self.telegram_user.imageswap_draft = {
            "workflow_name": "Flux Swap-Anything (Sam3.1)",
            "sam_prompt_text": "hairline and jaw",
            "_wizard_step": "confirm",
        }
        self.telegram_user.save(update_fields=["imageswap_draft"])
        update = self._build_callback_update("imageswap:edit:sam_prompt_text")
        context = self._build_context()

        self.async_run(self.command.imageswap_callback_query(update, context))

        self.telegram_user.refresh_from_db()
        self.assertEqual(self.telegram_user.imageswap_draft["_wizard_step"], "sam_prompt_text")
        reply = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("sam3.1 prompt section", reply.lower())

    def test_imageswap_repeat_uses_saved_defaults_without_reentering_values(self) -> None:
        generated_image = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="generated.png",
            file=ContentFile(b"generated-image", name="generated.png"),
        )
        defaults = {
            "workflow_name": "Flux Swap-Anything (Sam3.1)",
            "sam_prompt_text": "hairline and jaw",
            "positive_prompt": "portrait lighting",
            "swap_text_prompt": "studio portrait",
            "swap_reference_media_asset_id": generated_image.id,
            "target_media_asset_id": self.input_media.id,
        }
        self.telegram_user.imageswap_defaults = defaults
        self.telegram_user.save(update_fields=["imageswap_defaults"])
        self.command.oddesy_agent_service.build_imageswap_request = lambda user: {
            "job_payload": {
                "workflow_name": defaults["workflow_name"],
                "media_asset": self.input_media,
                "prompt": defaults["positive_prompt"],
                "metadata": {"imageswap": defaults},
            }
        }
        self.command.oddesy_agent_service.create_job_from_existing_media = lambda **kwargs: GenerationJob.objects.create(
            telegram_user=kwargs["telegram_user"],
            input_media=kwargs["media_asset"],
            workflow_name=kwargs["workflow_name"],
            prompt=kwargs["prompt"],
            metadata=kwargs["metadata"],
        )
        update = self._build_callback_update("imageswap:repeat")
        context = self._build_context()

        self.async_run(self.command.imageswap_callback_query(update, context))

        self.telegram_user.refresh_from_db()
        job = GenerationJob.objects.latest("id")
        self.assertEqual(job.prompt, "portrait lighting")
        self.assertEqual(self.telegram_user.imageswap_defaults["sam_prompt_text"], "hairline and jaw")
        self.assertEqual(self.telegram_user.imageswap_defaults["swap_text_prompt"], "studio portrait")
        self.assertEqual(self.telegram_user.imageswap_draft, {})

    def test_help_command_describes_image_and_video_modes(self) -> None:
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.help_command(update, context))

        reply = "\n".join(call.args[0] for call in update.message.reply_text.await_args_list)
        self.assertIn("/image <prompt>", reply)
        self.assertIn("/video <positive prompt>", reply)
        self.assertIn("/setworkflow <name>", reply)
        self.assertIn("/activeworkflow", reply)
        self.assertIn("/setvideoworkflow <name>", reply)
        self.assertIn("/activevideoworkflow", reply)
        self.assertIn("/imageloras", reply)
        self.assertIn("/videoloras", reply)
        self.assertIn("/setimagelora <slot> <on|off|default> [lora] [strength]", reply)
        self.assertIn("/setvideolora <slot> <on|off|default> [lora] [strength]", reply)
        self.assertIn("/setimagelorastrength <slot> <strength>", reply)
        self.assertIn("/setvideolorastrength <slot> <strength>", reply)
        self.assertIn("/imageswap", reply)
        self.assertIn("/imagepromptboxes", reply)
        self.assertIn("/videopromptboxes", reply)
        self.assertIn("/setimagepromptbox <field_key> <text>", reply)
        self.assertIn("/referencephotoimageset [media_id|lastupload|lastimage|clear]", reply)
        self.assertIn("/faceoutfitswapimageset [media_id|lastimage|clear]", reply)
        self.assertIn("/setvideopromptbox <field_key> <text>", reply)
        self.assertIn("/clearimagepromptbox <field_key>", reply)
        self.assertIn("/clearvideopromptbox <field_key>", reply)
        self.assertIn("/videonegative [prompt|clear]", reply)
        self.assertIn("/videolength [frames|clear]", reply)
        self.assertIn("/imagemode [saved|all]", reply)
        self.assertIn("/batchsize [count]", reply)
        self.assertIn("/lastimage", reply)
        self.assertIn("/videos", reply)
        self.assertIn("/getvideo <video_id>", reply)
        self.assertIn("/getvideojob <job_id>", reply)
        self.assertIn("/lastframe [upscale_factor] [sharpen_amount]", reply)
        self.assertIn("/lastframeid <video_id> [upscale_factor] [sharpen_amount]", reply)
        self.assertIn("/lastframeupscale [video_id]", reply)
        self.assertIn("/combinevideos [video_id_1] [video_id_2] [video_id_3] ...", reply)
        self.assertIn("/combinevideojobs <job_id_1> <job_id_2> [job_id_3] ...", reply)
        self.assertIn("/mvdhelp", reply)
        self.assertIn("/mvduse [project_id|clear]", reply)
        self.assertIn("/mvdstatus", reply)
        self.assertIn("/mvdgenerateprojectimages [project_id] [--limit <n>] [--tool <chatgpt>]", reply)
        self.assertIn("Music Video Director:", reply)
        self.assertIn("Text to image:", reply)
        self.assertIn("Image to image:", reply)
        self.assertIn("Image to video:", reply)
        self.assertIn(
            "Imageswap steps: Sam3.1 Prompt Section (text) -> Positive (text) -> Face or Outfit Prompt (text) -> Face or Outfit Referance (image) -> Referance Photo (image) -> confirm",
            reply,
        )
        self.assertIn("Use /lastframe to extract and enhance the last frame", reply)
        self.assertIn("Use /lastframeupscale [video_id] to queue a ComfyUI upscale", reply)

    def test_mvduse_command_sets_working_project(self) -> None:
        self.command.music_video_director_bridge.project_summary = Mock(
            return_value={"ok": True, "message": "Project loaded", "data": {"project_id": "proj-123"}}
        )
        update = self._build_update()
        context = self._build_context(args=["proj-123"])

        self.async_run(self.command.mvduse_command(update, context))

        self.telegram_user.refresh_from_db()
        self.assertEqual(self.telegram_user.musicvideo_working_project, "proj-123")
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "Music Video Director working project set to proj-123.",
        )

    def test_mvduse_command_clears_working_project(self) -> None:
        self.telegram_user.musicvideo_working_project = "proj-123"
        self.telegram_user.save(update_fields=["musicvideo_working_project"])
        update = self._build_update()
        context = self._build_context(args=["clear"])

        self.async_run(self.command.mvduse_command(update, context))

        self.telegram_user.refresh_from_db()
        self.assertEqual(self.telegram_user.musicvideo_working_project, "")
        self.assertEqual(update.message.reply_text.await_args.args[0], "Music Video Director working project cleared.")

    def test_mvdstatus_command_formats_project_summary(self) -> None:
        self.command.music_video_director_bridge.get_status = Mock(
            return_value={
                "ok": True,
                "message": "Status loaded",
                "data": {
                    "project_count": 2,
                    "active_projects": [
                        {
                            "project_id": "proj-123",
                            "song_title": "Test Song",
                            "artist_name": "Test Artist",
                            "current_step": "images",
                            "current_step_status": "in_progress",
                            "segments": 4,
                            "parts": 12,
                            "image_assets": 6,
                        }
                    ],
                },
            }
        )
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.mvdstatus_command(update, context))

        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("Projects: 2", reply)
        self.assertIn("proj-123 - Test Song by Test Artist", reply)
        self.assertIn("Step: images (in_progress)", reply)

    def test_mvdproject_command_uses_working_project(self) -> None:
        self.telegram_user.musicvideo_working_project = "proj-123"
        self.telegram_user.save(update_fields=["musicvideo_working_project"])
        self.command.music_video_director_bridge.project_summary = Mock(
            return_value={
                "ok": True,
                "message": "Project loaded",
                "data": {
                    "project_id": "proj-123",
                    "song_title": "Test Song",
                    "artist_name": "",
                    "current_step": "audio",
                    "current_step_status": "done",
                    "segments": 3,
                    "parts": 8,
                    "image_assets": 1,
                },
            }
        )
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.mvdproject_command(update, context))

        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "proj-123 - Test Song\nStep: audio (done)\nSegments: 3 | Parts: 8 | Images: 1",
        )

    def test_mvdclearstaleaudio_command_requests_approval(self) -> None:
        update = self._build_update()
        context = self._build_context(args=["proj-123"])

        self.async_run(self.command.mvdclearstaleaudio_command(update, context))

        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("Music Video Director approval required:", reply)
        self.assertIn("Clear stale audio refs for project proj-123", reply)
        reply_markup = update.message.reply_text.await_args.kwargs["reply_markup"]
        buttons = reply_markup.inline_keyboard[0]
        self.assertEqual(buttons[0].text, "Approve")
        self.assertTrue(buttons[0].callback_data.startswith("mvd:approve:"))

    def test_mvd_audio_caption_uses_working_project_without_caption(self) -> None:
        self.telegram_user.musicvideo_working_project = "proj-123"
        self.telegram_user.save(update_fields=["musicvideo_working_project"])

        result = self.command._parse_mvd_audio_caption(self.telegram_user, "")

        self.assertEqual(result, ("proj-123", "audio"))

    def test_checksegments_command_sends_preview_audio_for_working_project(self) -> None:
        self.telegram_user.musicvideo_working_project = "proj-123"
        self.telegram_user.save(update_fields=["musicvideo_working_project"])
        preview_file = Path(self._temp_media_root) / "segments-preview.mp3"
        preview_file.write_bytes(b"preview")
        self.command.music_video_director_bridge.get_segment_audio_preview_metadata = Mock(
            return_value={
                "ok": True,
                "message": "Segment preview metadata loaded",
                "data": {
                    "project_id": "proj-123",
                    "song_title": "Test Song",
                    "audio_path": str(preview_file),
                    "clips": [{"index": 0, "start_seconds": 0.0, "end_seconds": 2.0}],
                },
            }
        )
        self.command.mvd_audio_preview_service.build_preview = Mock(
            return_value={
                "ok": True,
                "audio_path": str(preview_file),
                "caption": "Test Song segments preview",
            }
        )
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.checksegments_command(update, context))

        self.command.music_video_director_bridge.get_segment_audio_preview_metadata.assert_called_once_with("proj-123")
        self.command.mvd_audio_preview_service.build_preview.assert_called_once()
        self.assertEqual(context.bot.send_audio.await_count, 1)
        self.assertEqual(context.bot.send_audio.await_args.kwargs["caption"], "Test Song segments preview")

    def test_checkparts_command_requires_visible_segment_number(self) -> None:
        self.telegram_user.musicvideo_working_project = "proj-123"
        self.telegram_user.save(update_fields=["musicvideo_working_project"])
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.checkparts_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Usage: /checkparts <segment_number>")

    def test_checkparts_command_sends_preview_audio_for_requested_segment(self) -> None:
        self.telegram_user.musicvideo_working_project = "proj-123"
        self.telegram_user.save(update_fields=["musicvideo_working_project"])
        preview_file = Path(self._temp_media_root) / "parts-preview.mp3"
        preview_file.write_bytes(b"preview")
        self.command.music_video_director_bridge.get_part_audio_preview_metadata = Mock(
            return_value={
                "ok": True,
                "message": "Part preview metadata loaded",
                "data": {
                    "project_id": "proj-123",
                    "song_title": "Test Song",
                    "segment_number": 5,
                    "audio_path": str(preview_file),
                    "clips": [{"index": 1, "start_seconds": 12.0, "end_seconds": 13.0}],
                },
            }
        )
        self.command.mvd_audio_preview_service.build_preview = Mock(
            return_value={
                "ok": True,
                "audio_path": str(preview_file),
                "caption": "Test Song segment 5 parts preview",
            }
        )
        update = self._build_update()
        context = self._build_context(args=["5"])

        self.async_run(self.command.checkparts_command(update, context))

        self.command.music_video_director_bridge.get_part_audio_preview_metadata.assert_called_once_with("proj-123", 5)
        self.command.mvd_audio_preview_service.build_preview.assert_called_once()
        self.assertEqual(context.bot.send_audio.await_count, 1)
        self.assertEqual(context.bot.send_audio.await_args.kwargs["caption"], "Test Song segment 5 parts preview")

    def test_mvdagent_result_formats_expected_namespaced_command(self) -> None:
        result = {
            "ok": True,
            "message": "Instruction interpreted",
            "data": {
                "proposed_command": "generate_project_images",
                "proposed_params": {"project_id": "proj-123", "limit": 4, "tool": "chatgpt"},
                "confidence": 0.91,
                "clarification_question": None,
            },
        }

        formatted = self.command._format_mvd_agent_result(result)

        self.assertIn("/mvdgenerateprojectimages proj-123 --limit 4 --tool chatgpt", formatted)
        self.assertIn("Confidence: 91%", formatted)

    def test_image_command_queues_prompt_only_job(self) -> None:
        def create_prompt_only_job(**kwargs):
            return GenerationJob.objects.create(
                telegram_user=kwargs["telegram_user"],
                input_media=None,
                workflow_name="jugg_latent_cyberpony (1)",
                prompt=kwargs["prompt"],
                seed=kwargs["seed"],
                metadata=kwargs["metadata"],
            )

        self.command.oddesy_agent_service.create_job_from_prompt = create_prompt_only_job
        update = self._build_update()
        context = self._build_context(args=["cyberpunk", "alley", "portrait"])

        self.async_run(self.command.image_command(update, context))

        job = GenerationJob.objects.latest("id")
        self.assertIsNone(job.input_media_id)
        self.assertEqual(job.prompt, "cyberpunk alley portrait")
        self.assertEqual(job.workflow_name, "jugg_latent_cyberpony (1)")
        self.assertEqual(update.message.reply_text.await_args.args[0], f"Queued job #{job.id}.")

    def test_image_command_requires_explicit_source_image_when_workflow_requires_input_media(self) -> None:
        self.command.oddesy_agent_service.get_active_text_workflow = lambda user: "Flux Swap-Anything (Sam3.1)"
        self.command.oddesy_agent_service.workflow_requires_input_media = (
            lambda workflow_name: workflow_name == "Flux Swap-Anything (Sam3.1)"
        )
        update = self._build_update()
        context = self._build_context(args=["swap", "the", "face"])

        self.async_run(self.command.image_command(update, context))

        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "That image workflow needs an explicit source image. Upload an image, then use /referencephotoimageset lastupload, or set a specific image with /referencephotoimageset <media_id>.",
        )

    def test_image_command_queues_job_from_explicit_reference_photo_override(self) -> None:
        self.command.oddesy_agent_service.get_active_text_workflow = lambda user: "Flux Swap-Anything (Sam3.1)"
        self.command.oddesy_agent_service.workflow_requires_input_media = (
            lambda workflow_name: workflow_name == "Flux Swap-Anything (Sam3.1)"
        )
        self.command._resolve_reference_photo_field_key = lambda workflow: "referance_photo"
        self.command.oddesy_agent_service.get_workflow_image_overrides = lambda user, workflow: {
            "referance_photo": self.input_media.id
        }

        def create_image_conditioned_job(**kwargs):
            return GenerationJob.objects.create(
                telegram_user=kwargs["telegram_user"],
                input_media=kwargs["media_asset"],
                workflow_name=kwargs["workflow_name"],
                prompt=kwargs["prompt"],
                seed=kwargs["seed"],
                metadata=kwargs["metadata"],
            )

        self.command.oddesy_agent_service.create_job_from_existing_media = create_image_conditioned_job
        update = self._build_update()
        context = self._build_context(args=["swap", "the", "face"])

        self.async_run(self.command.image_command(update, context))

        job = GenerationJob.objects.latest("id")
        self.assertEqual(job.input_media_id, self.input_media.id)
        self.assertEqual(job.workflow_name, "Flux Swap-Anything (Sam3.1)")
        self.assertEqual(job.prompt, "swap the face")
        self.assertEqual(update.message.reply_text.await_args.args[0], f"Queued job #{job.id}.")

    def test_video_command_queues_video_job_with_saved_settings(self) -> None:
        self.command.oddesy_agent_service.get_active_video_workflow = lambda user: "LTX2.3 I2V 4060 Optimised - PRN"
        self.command.oddesy_agent_service.get_active_video_negative_prompt = lambda user: "blurry, low quality"
        self.command.oddesy_agent_service.get_active_video_length_frames = lambda user: 120
        self.command.oddesy_agent_service.workflow_requires_input_media = lambda workflow_name: workflow_name == "LTX2.3 I2V 4060 Optimised - PRN"

        def create_video_job(**kwargs):
            return GenerationJob.objects.create(
                telegram_user=kwargs["telegram_user"],
                input_media=kwargs["media_asset"],
                workflow_name=kwargs["workflow_name"],
                prompt=kwargs["prompt"],
                seed=kwargs["seed"],
                metadata=kwargs["metadata"],
            )

        self.command.oddesy_agent_service.create_job_from_existing_media = create_video_job
        update = self._build_update()
        context = self._build_context(args=["dramatic", "camera", "push-in"])

        self.async_run(self.command.video_command(update, context))

        job = GenerationJob.objects.latest("id")
        self.assertEqual(job.workflow_name, "LTX2.3 I2V 4060 Optimised - PRN")
        self.assertEqual(job.prompt, "dramatic camera push-in")
        self.assertEqual(job.metadata["parsed_instruction"]["negative_prompt"], "blurry, low quality")
        self.assertEqual(job.metadata["parsed_instruction"]["length_frames"], 120)
        self.assertEqual(update.message.reply_text.await_args.args[0], f"Queued job #{job.id}.")

    def test_image_command_queues_multiple_prompt_only_jobs_when_batch_size_is_greater_than_one(self) -> None:
        self.command.oddesy_agent_service.get_generation_batch_count = lambda user: 3

        def create_prompt_only_job(**kwargs):
            return GenerationJob.objects.create(
                telegram_user=kwargs["telegram_user"],
                input_media=None,
                workflow_name="jugg_latent_cyberpony (1)",
                prompt=kwargs["prompt"],
                seed=kwargs["seed"],
                metadata=kwargs["metadata"],
            )

        self.command.oddesy_agent_service.create_job_from_prompt = create_prompt_only_job
        update = self._build_update()
        context = self._build_context(args=["cyberpunk", "alley", "portrait"])

        self.async_run(self.command.image_command(update, context))

        jobs = list(GenerationJob.objects.order_by("id"))
        self.assertEqual(len(jobs), 3)
        self.assertEqual([job.metadata["batch"]["index"] for job in jobs], [1, 2, 3])
        self.assertEqual(jobs[0].metadata["batch"]["count"], 3)
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            f"Queued 3 jobs: #{jobs[0].id}, #{jobs[1].id}, #{jobs[2].id}.",
        )

    def test_setworkflow_command_updates_active_workflow(self) -> None:
        self.command.oddesy_agent_service.set_active_text_workflow = lambda user, workflow: "jugg_latent_cyberpony (1)"
        update = self._build_update()
        context = self._build_context(args=["jugg_latent_cyberpony"])

        self.async_run(self.command.setworkflow_command(update, context))

        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "Active image workflow set to: jugg_latent_cyberpony (1)",
        )

    def test_setvideoworkflow_command_updates_active_workflow(self) -> None:
        self.command.oddesy_agent_service.set_active_video_workflow = lambda user, workflow: "LTX2.3 I2V 4060 Optimised - PRN"
        update = self._build_update()
        context = self._build_context(args=["LTX2.3", "I2V", "4060", "Optimised", "-", "PRN"])

        self.async_run(self.command.setvideoworkflow_command(update, context))

        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "Active video workflow set to: LTX2.3 I2V 4060 Optimised - PRN",
        )

    def test_activeworkflow_command_reports_active_workflow(self) -> None:
        self.command.oddesy_agent_service.get_active_text_workflow = lambda user: "jugg_latent_cyberpony (1)"
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.activeworkflow_command(update, context))

        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "Active image workflow: jugg_latent_cyberpony (1)",
        )

    def test_activevideoworkflow_command_reports_active_workflow(self) -> None:
        self.command.oddesy_agent_service.get_active_video_workflow = lambda user: "LTX2.3 I2V 4060 Optimised - PRN"
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.activevideoworkflow_command(update, context))

        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "Active video workflow: LTX2.3 I2V 4060 Optimised - PRN",
        )

    def test_imageloras_command_reports_effective_slots(self) -> None:
        self.command.oddesy_agent_service.get_active_text_workflow = lambda user: "jugg_latent_cyberpony (1)"
        self.command.oddesy_agent_service.get_effective_power_lora_slots = lambda user, workflow: [
            {"slot": 1, "on": True, "lora": "pony\\a.safetensors", "strength": 1.0, "overridden": False},
            {"slot": 2, "on": False, "lora": "pony\\b.safetensors", "strength": 0.5, "overridden": True},
        ]
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.imageloras_command(update, context))

        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("Active image workflow LoRAs: jugg_latent_cyberpony (1)", reply)
        self.assertIn("Slot 1: on | pony\\a.safetensors | strength=1.0", reply)
        self.assertIn("Slot 2: off | pony\\b.safetensors | strength=0.5 override", reply)

    def test_setvideolora_command_updates_slot(self) -> None:
        self.command.oddesy_agent_service.get_active_video_workflow = lambda user: "LTX2.3 I2V 4060 Optimised - PRN"
        self.command.oddesy_agent_service.set_workflow_lora_override = lambda *args, **kwargs: {"on": True}
        self.command.oddesy_agent_service.get_effective_power_lora_slots = lambda user, workflow: [
            {"slot": 3, "on": True, "lora": "ltx23\\override.safetensors", "strength": 0.8, "overridden": True}
        ]
        update = self._build_update()
        context = self._build_context(args=["3", "on", "ltx23\\override.safetensors", "0.8"])

        self.async_run(self.command.setvideolora_command(update, context))

        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "Video workflow LoRA slot 3: on | ltx23\\override.safetensors | strength=0.8",
        )

    def test_setimagelorastrength_command_updates_slot_strength_only(self) -> None:
        self.command.oddesy_agent_service.get_active_text_workflow = lambda user: "jugg_latent_cyberpony (1)"
        captured = {}

        def set_override(user, workflow, slot, **kwargs):
            captured["workflow"] = workflow
            captured["slot"] = slot
            captured["kwargs"] = kwargs
            return kwargs

        self.command.oddesy_agent_service.set_workflow_lora_override = set_override
        self.command.oddesy_agent_service.get_effective_power_lora_slots = lambda user, workflow: [
            {"slot": 2, "on": True, "lora": "pony\\a.safetensors", "strength": 0.65, "overridden": True}
        ]
        update = self._build_update()
        context = self._build_context(args=["2", "0.65"])

        self.async_run(self.command.setimagelorastrength_command(update, context))

        self.assertEqual(captured["workflow"], "jugg_latent_cyberpony (1)")
        self.assertEqual(captured["slot"], 2)
        self.assertEqual(captured["kwargs"], {"strength": 0.65})
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "Image workflow LoRA slot 2: on | pony\\a.safetensors | strength=0.65",
        )

    def test_setvideolorastrength_command_rejects_non_numeric_strength(self) -> None:
        self.command.oddesy_agent_service.get_active_video_workflow = lambda user: "LTX2.3 I2V 4060 Optimised - PRN"
        update = self._build_update()
        context = self._build_context(args=["3", "high"])

        self.async_run(self.command.setvideolorastrength_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "LoRA strength must be a number.")

    def test_imagepromptboxes_command_lists_workflow_text_fields(self) -> None:
        self.command.oddesy_agent_service.get_active_text_workflow = lambda user: "Flux Swap-Anything (Sam3.1)"
        self.command.oddesy_agent_service.get_effective_workflow_text_fields = lambda user, workflow: [
            {
                "key": "sam3_1_prompt_section_a",
                "label": "Sam3.1 Prompt Section A",
                "value": "female hair",
                "overridden": False,
            },
            {
                "key": "face_or_outfit_prompt",
                "label": "Face or Outfit Prompt",
                "value": "face, hair",
                "overridden": True,
            },
        ]
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.imagepromptboxes_command(update, context))

        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("Active image workflow text boxes: Flux Swap-Anything (Sam3.1)", reply)
        self.assertIn("sam3_1_prompt_section_a: Sam3.1 Prompt Section A = female hair", reply)
        self.assertIn("face_or_outfit_prompt: Face or Outfit Prompt = face, hair override", reply)

    def test_setimagepromptbox_command_updates_text_field(self) -> None:
        self.command.oddesy_agent_service.get_active_text_workflow = lambda user: "Flux Swap-Anything (Sam3.1)"
        captured = {}

        def set_override(user, workflow, key, value):
            captured["workflow"] = workflow
            captured["key"] = key
            captured["value"] = value
            return value

        self.command.oddesy_agent_service.set_workflow_text_override = set_override
        update = self._build_update()
        context = self._build_context(args=["face_or_outfit_prompt", "outfit,", "accessories"])

        self.async_run(self.command.setimagepromptbox_command(update, context))

        self.assertEqual(captured["workflow"], "Flux Swap-Anything (Sam3.1)")
        self.assertEqual(captured["key"], "face_or_outfit_prompt")
        self.assertEqual(captured["value"], "outfit, accessories")
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "Image workflow text box 'face_or_outfit_prompt' updated.",
        )

    def test_faceoutfitswapimageset_command_sets_latest_generated_image_override(self) -> None:
        self.command.oddesy_agent_service.get_active_text_workflow = lambda user: "Flux Swap-Anything (Sam3.1)"
        self.command.oddesy_agent_service.list_workflow_image_fields = lambda workflow: [
            {"key": "face_or_outfit_referance", "label": "Face or Outfit Referance"},
            {"key": "referance_photo", "label": "Referance Photo"},
        ]
        generated_image = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="generated.png",
            file=ContentFile(b"generated-image", name="generated.png"),
        )
        self.command.oddesy_agent_service.get_latest_generated_image = lambda user: generated_image
        captured = {}

        def set_image_override(user, workflow, key, media_asset_id):
            captured["workflow"] = workflow
            captured["key"] = key
            captured["media_asset_id"] = media_asset_id
            return media_asset_id

        self.command.oddesy_agent_service.set_workflow_image_override = set_image_override
        update = self._build_update()
        context = self._build_context(args=["lastimage"])

        self.async_run(self.command.faceoutfitswapimageset_command(update, context))

        self.assertEqual(captured["workflow"], "Flux Swap-Anything (Sam3.1)")
        self.assertEqual(captured["key"], "face_or_outfit_referance")
        self.assertEqual(captured["media_asset_id"], generated_image.id)
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            f"Face or Outfit Referance image set to #{generated_image.id}: generated.png",
        )

    def test_referencephotoimageset_command_sets_latest_uploaded_image_override(self) -> None:
        self.command.oddesy_agent_service.get_active_text_workflow = lambda user: "Flux Swap-Anything (Sam3.1)"
        self.command.oddesy_agent_service.list_workflow_image_fields = lambda workflow: [
            {"key": "face_or_outfit_referance", "label": "Face or Outfit Referance"},
            {"key": "referance_photo", "label": "Referance Photo"},
        ]
        captured = {}

        def set_image_override(user, workflow, key, media_asset_id):
            captured["workflow"] = workflow
            captured["key"] = key
            captured["media_asset_id"] = media_asset_id
            return media_asset_id

        self.command.oddesy_agent_service.set_workflow_image_override = set_image_override
        update = self._build_update()
        context = self._build_context(args=["lastupload"])

        self.async_run(self.command.referencephotoimageset_command(update, context))

        self.assertEqual(captured["workflow"], "Flux Swap-Anything (Sam3.1)")
        self.assertEqual(captured["key"], "referance_photo")
        self.assertEqual(captured["media_asset_id"], self.input_media.id)
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            f"Referance Photo image set to #{self.input_media.id}: input.jpg",
        )

    def test_clearvideopromptbox_command_clears_text_field(self) -> None:
        self.command.oddesy_agent_service.get_active_video_workflow = lambda user: "Flux Swap-Anything (Sam3.1)"
        captured = {}

        def clear_override(user, workflow, key):
            captured["workflow"] = workflow
            captured["key"] = key

        self.command.oddesy_agent_service.clear_workflow_text_override = clear_override
        update = self._build_update()
        context = self._build_context(args=["sam3_1_prompt_section_b"])

        self.async_run(self.command.clearvideopromptbox_command(update, context))

        self.assertEqual(captured["workflow"], "Flux Swap-Anything (Sam3.1)")
        self.assertEqual(captured["key"], "sam3_1_prompt_section_b")
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "Video workflow text box 'sam3_1_prompt_section_b' cleared.",
        )

    def test_imagemode_command_reports_current_mode(self) -> None:
        self.command.oddesy_agent_service.get_image_output_mode = lambda user: "saved"
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.imagemode_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Image output mode: saved")

    def test_imagemode_command_updates_mode(self) -> None:
        self.command.oddesy_agent_service.set_image_output_mode = lambda user, mode: "all"
        update = self._build_update()
        context = self._build_context(args=["all"])

        self.async_run(self.command.imagemode_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Image output mode set to: all")

    def test_batchsize_command_reports_current_count(self) -> None:
        self.command.oddesy_agent_service.get_generation_batch_count = lambda user: 1
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.batchsize_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Generation batch size: 1")

    def test_batchsize_command_updates_count(self) -> None:
        self.command.oddesy_agent_service.set_generation_batch_count = lambda user, count: 4
        update = self._build_update()
        context = self._build_context(args=["4"])

        self.async_run(self.command.batchsize_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Generation batch size set to: 4")

    def test_videonegative_command_reports_workflow_default_when_blank(self) -> None:
        self.command.oddesy_agent_service.get_active_video_negative_prompt = lambda user: ""
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.videonegative_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Active video negative prompt: workflow default")

    def test_videonegative_command_updates_prompt(self) -> None:
        self.command.oddesy_agent_service.set_active_video_negative_prompt = lambda user, prompt: "blurry, low quality"
        update = self._build_update()
        context = self._build_context(args=["blurry,", "low", "quality"])

        self.async_run(self.command.videonegative_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Active video negative prompt updated.")

    def test_videolength_command_reports_workflow_default_when_blank(self) -> None:
        self.command.oddesy_agent_service.get_active_video_length_frames = lambda user: None
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.videolength_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Active video length: workflow default")

    def test_videolength_command_updates_frames(self) -> None:
        self.command.oddesy_agent_service.set_active_video_length_frames = lambda user, value: 120
        update = self._build_update()
        context = self._build_context(args=["120"])

        self.async_run(self.command.videolength_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Active video length set to: 120 frames")

    def test_lastimage_command_sends_latest_generated_image(self) -> None:
        image_asset = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="output.png",
            file=ContentFile(b"image-bytes", name="output.png"),
        )
        self.command.oddesy_agent_service.get_latest_generated_image = lambda user: image_asset
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.lastimage_command(update, context))

        context.bot.send_document.assert_awaited()

    def test_lastframe_command_sends_enhanced_last_frame_document(self) -> None:
        enhanced_asset = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="output_last_frame_enhanced.png",
            file=ContentFile(b"image-bytes", name="output_last_frame_enhanced.png"),
        )
        self.command.oddesy_agent_service.enhance_latest_video_last_frame = (
            lambda user, upscale_factor=2.0, sharpen_amount=0.4: enhanced_asset
        )
        update = self._build_update()
        context = self._build_context(args=["2.5", "0.2"])

        self.async_run(self.command.lastframe_command(update, context))

        context.bot.send_chat_action.assert_awaited()
        context.bot.send_document.assert_awaited()
        self.assertIn("upscale=2.5, sharpen=0.2", context.bot.send_document.await_args.kwargs["caption"])

    def test_lastframeid_command_targets_specific_video_id(self) -> None:
        enhanced_asset = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="output_last_frame_enhanced.png",
            file=ContentFile(b"image-bytes", name="output_last_frame_enhanced.png"),
        )
        captured = {}

        def enhance_by_id(user, video_id, upscale_factor=2.0, sharpen_amount=0.4):
            captured["video_id"] = video_id
            captured["upscale_factor"] = upscale_factor
            captured["sharpen_amount"] = sharpen_amount
            return enhanced_asset

        self.command.oddesy_agent_service.enhance_video_last_frame_by_id = enhance_by_id
        update = self._build_update()
        context = self._build_context(args=["99", "2.2", "0.1"])

        self.async_run(self.command.lastframeid_command(update, context))

        self.assertEqual(captured["video_id"], 99)
        self.assertEqual(captured["upscale_factor"], 2.2)
        self.assertEqual(captured["sharpen_amount"], 0.1)

    def test_lastframe_command_reports_missing_video(self) -> None:
        self.command.oddesy_agent_service.enhance_latest_video_last_frame = (
            lambda user, upscale_factor=2.0, sharpen_amount=0.4: None
        )
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.lastframe_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "No saved video found.")

    def test_lastframe_command_validates_arguments(self) -> None:
        update = self._build_update()
        context = self._build_context(args=["1"])

        self.async_run(self.command.lastframe_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Upscale factor must be greater than 1.")

    def test_lastframeupscale_command_queues_job_for_latest_video(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="Oddesy Last Frame Upscale",
            prompt="",
            metadata={"last_frame_upscale": {"source_video_media_asset_id": 77}},
        )
        self.command.oddesy_agent_service.queue_last_frame_upscale_job = lambda user, media_asset_id=None: job
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.lastframeupscale_command(update, context))

        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            f"Queued job #{job.id} for last-frame upscale from video #77.",
        )

    def test_lastframeupscale_command_validates_video_id(self) -> None:
        update = self._build_update()
        context = self._build_context(args=["abc"])

        self.async_run(self.command.lastframeupscale_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Video id must be a whole number.")

    def test_text_message_routes_lastframeupscale_fallback(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="Oddesy Last Frame Upscale",
            prompt="",
            metadata={"last_frame_upscale": {"source_video_media_asset_id": 157}},
        )
        self.command.instruction_parser.parse_text = lambda text: ParsedIntent(
            action="lastframeupscale",
            job_id=157,
            message="lastframeupscale",
            metadata={"parser": "fallback"},
        )
        self.command.oddesy_agent_service.queue_last_frame_upscale_job = lambda user, media_asset_id=None: job
        update = self._build_update()
        update.message.text = "lastframeupscale 157"
        context = self._build_context()

        self.async_run(self.command.text_message(update, context))

        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            f"Queued job #{job.id} for last-frame upscale from video #157.",
        )

    def test_videos_command_lists_recent_saved_videos(self) -> None:
        uploaded_video = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_VIDEO,
            original_file_name="upload.mp4",
            file=ContentFile(b"video-bytes", name="upload.mp4"),
        )
        generated_video = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name="output.mp4",
            file=ContentFile(b"video-bytes", name="output.mp4"),
        )
        self.command.oddesy_agent_service.list_recent_videos = lambda user, limit=10: [generated_video, uploaded_video]
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.videos_command(update, context))

        reply = update.message.reply_text.await_args.args[0]
        self.assertIn(f"#{generated_video.id} | generated_video | output.mp4", reply)
        self.assertIn(f"#{uploaded_video.id} | incoming_video | upload.mp4", reply)

    def test_combinevideos_command_sends_combined_video(self) -> None:
        combined_video = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name="combined.mp4",
            file=ContentFile(b"video-bytes", name="combined.mp4"),
        )
        captured = {}

        def combine_by_ids(user, video_ids):
            captured["video_ids"] = video_ids
            return combined_video

        self.command.oddesy_agent_service.combine_videos_by_ids = combine_by_ids
        update = self._build_update()
        context = self._build_context(args=["10", "11", "12"])

        self.async_run(self.command.combinevideos_command(update, context))

        self.assertEqual(captured["video_ids"], [10, 11, 12])
        self.assertEqual(update.message.reply_text.await_args_list[0].args[0], "Combine request received for 3 video ids.")
        context.bot.send_video.assert_awaited()

    def test_combinevideojobs_command_sends_combined_video(self) -> None:
        combined_video = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name="combined-jobs.mp4",
            file=ContentFile(b"video-bytes", name="combined-jobs.mp4"),
        )
        captured = {}

        def combine_by_job_ids(user, job_ids):
            captured["job_ids"] = job_ids
            return combined_video

        self.command.oddesy_agent_service.combine_videos_by_job_ids = combine_by_job_ids
        update = self._build_update()
        context = self._build_context(args=["85", "86", "87"])

        self.async_run(self.command.combinevideojobs_command(update, context))

        self.assertEqual(captured["job_ids"], [85, 86, 87])
        self.assertEqual(update.message.reply_text.await_args_list[0].args[0], "Combine request received for 3 job ids.")
        context.bot.send_video.assert_awaited()

    def test_combinevideojobs_command_falls_back_to_document_when_video_send_fails(self) -> None:
        combined_video = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name="combined-jobs.mp4",
            file=ContentFile(b"video-bytes", name="combined-jobs.mp4"),
        )
        self.command.oddesy_agent_service.combine_videos_by_job_ids = lambda user, job_ids: combined_video
        update = self._build_update()
        context = self._build_context(args=["85", "86"])
        context.bot.send_video.side_effect = RuntimeError("send_video failed")

        self.async_run(self.command.combinevideojobs_command(update, context))

        context.bot.send_document.assert_awaited()

    def test_video_message_saves_uploaded_video(self) -> None:
        update = self._build_update()
        update.message.video = SimpleNamespace(
            file_id="telegram-video-file-id",
            file_unique_id="video-unique-id",
            file_name="clip.mp4",
        )
        context = self._build_context()
        context.bot.get_file.return_value = SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"video-bytes"))
        )

        self.async_run(self.command.video_message(update, context))

        asset = MediaAsset.objects.latest("id")
        self.assertEqual(asset.asset_type, MediaAsset.TYPE_INCOMING_VIDEO)
        self.assertEqual(asset.original_file_name, "clip.mp4")
        self.assertIn("Video saved as", update.message.reply_text.await_args.args[0])

    def test_getvideo_command_sends_requested_video(self) -> None:
        video_asset = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name="clip.mp4",
            file=ContentFile(b"video-bytes", name="clip.mp4"),
        )
        update = self._build_update()
        context = self._build_context(args=[str(video_asset.id)])

        self.async_run(self.command.getvideo_command(update, context))

        context.bot.send_video.assert_awaited()
        self.assertEqual(
            context.bot.send_video.await_args.kwargs["caption"],
            f"Video #{video_asset.id}",
        )

    def test_getvideo_command_rejects_missing_video(self) -> None:
        update = self._build_update()
        context = self._build_context(args=["999999"])

        self.async_run(self.command.getvideo_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Saved video #999999 was not found.")

    def test_getvideojob_command_sends_video_for_job_output(self) -> None:
        video_asset = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name="job-video.mp4",
            file=ContentFile(b"video-bytes", name="job-video.mp4"),
        )
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            output_media=video_asset,
            workflow_name="workflow_a",
            prompt="done",
            seed=1,
        )
        update = self._build_update()
        context = self._build_context(args=[str(job.id)])

        self.async_run(self.command.getvideojob_command(update, context))

        context.bot.send_video.assert_awaited()
        self.assertEqual(
            context.bot.send_video.await_args.kwargs["caption"],
            f"Video from job #{job.id}: #{video_asset.id}",
        )

    def test_getvideojob_command_rejects_job_without_video_output(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="done",
            seed=1,
        )
        update = self._build_update()
        context = self._build_context(args=[str(job.id)])

        self.async_run(self.command.getvideojob_command(update, context))

        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            f"Job #{job.id} was not found or has no saved output video.",
        )

    def test_workflows_command_includes_active_workflow(self) -> None:
        self.command.oddesy_agent_service.list_workflows = lambda: ["workflow_a", "workflow_b"]
        self.command.oddesy_agent_service.get_active_text_workflow = lambda user: "workflow_b"
        self.command.oddesy_agent_service.get_active_video_workflow = lambda user: "workflow_a"
        update = self._build_update()
        context = self._build_context()

        self.async_run(self.command.workflows_command(update, context))

        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("Active image workflow: workflow_b", reply)
        self.assertIn("Active video workflow: workflow_a", reply)
        self.assertIn("workflow_a", reply)
        self.assertIn("workflow_b", reply)

    def test_image_command_requires_prompt(self) -> None:
        update = self._build_update()
        context = self._build_context(args=[])

        self.async_run(self.command.image_command(update, context))

        self.assertEqual(update.message.reply_text.await_args.args[0], "Usage: /image <prompt>")

    def test_text_message_queues_prompt_only_job_without_using_latest_image(self) -> None:
        self.command.instruction_parser.parse_text = lambda text: ParsedIntent(
            action="create_job",
            workflow_name="jugg_latent_cyberpony",
            prompt="cyberpunk alley portrait",
            metadata={
                "parser": "fallback",
                "parsed_instruction": {
                    "workflow": "jugg_latent_cyberpony",
                    "prompt": "cyberpunk alley portrait",
                    "seed": 0,
                    "duration": None,
                    "motion": None,
                    "raw_text": text,
                },
            },
        )
        def create_prompt_only_job(**kwargs):
            return GenerationJob.objects.create(
                telegram_user=kwargs["telegram_user"],
                input_media=None,
                workflow_name="jugg_latent_cyberpony (1)",
                prompt=kwargs["prompt"],
                seed=kwargs["seed"],
                metadata=kwargs["metadata"],
            )

        self.command.oddesy_agent_service.create_job_from_prompt = create_prompt_only_job
        update = self._build_update()
        update.message.text = "cyberpunk alley portrait"
        context = self._build_context()

        self.async_run(self.command.text_message(update, context))

        job = GenerationJob.objects.latest("id")
        self.assertIsNone(job.input_media_id)
        self.assertEqual(job.workflow_name, "jugg_latent_cyberpony (1)")
        self.assertEqual(job.prompt, "cyberpunk alley portrait")
        self.assertEqual(update.message.reply_text.await_args.args[0], f"Queued job #{job.id}.")

    def test_text_message_uses_pending_uploaded_image_for_video_prompt(self) -> None:
        self.telegram_user.pending_video_media_asset_id = self.input_media.id
        self.telegram_user.save(update_fields=["pending_video_media_asset_id"])
        self.command.instruction_parser.parse_text = lambda text: ParsedIntent(
            action="create_job",
            workflow_name="jugg_latent_cyberpony",
            prompt="cinematic rain walk",
            metadata={
                "parser": "fallback",
                "parsed_instruction": {
                    "workflow": "jugg_latent_cyberpony",
                    "prompt": "cinematic rain walk",
                    "seed": 0,
                    "duration": None,
                    "motion": None,
                    "raw_text": text,
                },
            },
        )
        update = self._build_update()
        update.message.text = "cinematic rain walk"
        context = self._build_context()

        self.async_run(self.command.text_message(update, context))

        job = GenerationJob.objects.latest("id")
        self.telegram_user.refresh_from_db()
        self.assertEqual(job.input_media_id, self.input_media.id)
        self.assertEqual(job.workflow_name, "i2v_wan_480p")
        self.assertEqual(job.prompt, "cinematic rain walk")
        self.assertIsNone(self.telegram_user.pending_video_media_asset_id)
        self.assertEqual(update.message.reply_text.await_args.args[0], f"Queued job #{job.id}.")

    def test_text_message_routes_status_intent_without_queuing_job(self) -> None:
        existing_job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_a",
            prompt="done",
            seed=9,
        )
        existing_job.mark_running()
        self.command.instruction_parser.parse_text = lambda text: ParsedIntent(
            action="status",
            message="status",
            metadata={"parser": "fallback"},
        )
        update = self._build_update()
        update.message.text = "status"
        context = self._build_context()

        self.async_run(self.command.text_message(update, context))

        self.assertEqual(GenerationJob.objects.count(), 1)
        reply = update.message.reply_text.await_args.args[0]
        self.assertIn(f"Job #{existing_job.id}: running", reply)

    def async_run(self, coroutine):
        import asyncio

        return asyncio.run(coroutine)
