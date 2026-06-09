from types import SimpleNamespace
from unittest.mock import AsyncMock

from django.core.files.base import ContentFile
from django.test import TransactionTestCase, override_settings

from apps.core.management.commands.run_telegram_bot import Command
from apps.core.models import GenerationJob, MediaAsset, TelegramUser
from apps.core.services.instruction_parser import ParsedIntent


@override_settings(TELEGRAM_ALLOWED_USER_IDS=[12345])
class RunTelegramBotCommandTests(TransactionTestCase):
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
        self.input_media = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name="input.jpg",
            telegram_file_id="telegram-file-1",
            file=ContentFile(b"image-bytes", name="input.jpg"),
        )

    def _build_update(self):
        message = SimpleNamespace(reply_text=AsyncMock())
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
                send_chat_action=AsyncMock(),
                send_video=AsyncMock(),
                send_document=AsyncMock(),
            ),
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
