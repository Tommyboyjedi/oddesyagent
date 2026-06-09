from __future__ import annotations

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from apps.core.models import AuditLog, GenerationJob, MediaAsset, TelegramUser
from apps.core.services.instruction_parser import InstructionParserService, ParsedIntent
from apps.core.services.job_service import JobService
from apps.core.services.oddesy_agent_service import OddesyAgentService


class Command(BaseCommand):
    help = "Run the Telegram bot that accepts images and queues generation jobs."

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.job_service = JobService()
        self.oddesy_agent_service = OddesyAgentService(job_service=self.job_service)
        self.instruction_parser = InstructionParserService()

    def handle(self, *args, **options) -> None:
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN is not configured")

        application = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("status", self.status_command))
        application.add_handler(CommandHandler("workflows", self.workflows_command))
        application.add_handler(CommandHandler("queue", self.queue_command))
        application.add_handler(CommandHandler("history", self.history_command))
        application.add_handler(CommandHandler("rerun", self.rerun_command))
        application.add_handler(CommandHandler("last", self.last_command))
        application.add_handler(CommandHandler("cancel", self.cancel_command))
        application.add_handler(MessageHandler(filters.PHOTO, self.photo_message))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))

        self.stdout.write(self.style.SUCCESS("Telegram bot started"))
        application.run_polling()

    async def get_or_reject_user(self, update: Update) -> TelegramUser | None:
        return await sync_to_async(self._get_or_reject_user_sync, thread_sensitive=True)(update)

    def _get_or_reject_user_sync(self, update: Update) -> TelegramUser | None:
        tg_user = update.effective_user
        if tg_user is None:
            return None

        is_allowed = tg_user.id in settings.TELEGRAM_ALLOWED_USER_IDS
        telegram_user, _ = TelegramUser.objects.update_or_create(
            telegram_user_id=tg_user.id,
            defaults={
                "username": tg_user.username or "",
                "first_name": tg_user.first_name or "",
                "last_name": tg_user.last_name or "",
                "is_allowed": is_allowed,
            },
        )

        if is_allowed:
            return telegram_user

        AuditLog.objects.create(
            event_type="access_rejected",
            telegram_user=telegram_user,
            message="Rejected Telegram user",
            metadata={"telegram_user_id": tg_user.id},
        )
        return None

    async def log_event(
        self,
        event_type: str,
        message: str,
        telegram_user: TelegramUser | None = None,
        generation_job: GenerationJob | None = None,
        metadata: dict | None = None,
    ) -> None:
        await sync_to_async(self._log_event_sync, thread_sensitive=True)(
            event_type,
            message,
            telegram_user,
            generation_job,
            metadata,
        )

    def _log_event_sync(
        self,
        event_type: str,
        message: str,
        telegram_user: TelegramUser | None = None,
        generation_job: GenerationJob | None = None,
        metadata: dict | None = None,
    ) -> None:
        AuditLog.objects.create(
            event_type=event_type,
            telegram_user=telegram_user,
            generation_job=generation_job,
            message=message,
            metadata=metadata or {},
        )

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/start", telegram_user=telegram_user)
        await update.message.reply_text(
            "OddesyAgent is ready. Send an image, then send 'make video' or describe the video you want."
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/help", telegram_user=telegram_user)
        await update.message.reply_text(
            "/start\n/help\n/status\n/workflows\n/queue\n/history\n/rerun [job_id]\n/last\n/cancel\n\n"
            "Send an image, then send 'make video' or describe the video you want."
        )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/status", telegram_user=telegram_user)
        job = await sync_to_async(
            lambda: telegram_user.generation_jobs.order_by("-created_at").first(),
            thread_sensitive=True,
        )()
        if job is None:
            await update.message.reply_text("No jobs found.")
            return
        await update.message.reply_text(self._format_status_message(job))

    async def workflows_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/workflows", telegram_user=telegram_user)
        workflows = await sync_to_async(self.oddesy_agent_service.list_workflows, thread_sensitive=True)()
        await update.message.reply_text("\n".join(workflows) if workflows else "No workflows found.")

    async def queue_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/queue", telegram_user=telegram_user)
        jobs = await sync_to_async(
            lambda: list(
                telegram_user.generation_jobs.filter(
                    state__in=[
                        GenerationJob.STATE_QUEUED,
                        GenerationJob.STATE_RUNNING,
                        GenerationJob.STATE_CANCELLATION_REQUESTED,
                    ]
                )
                .order_by("created_at")[:10]
            ),
            thread_sensitive=True,
        )()
        if not jobs:
            await update.message.reply_text("No queued or running jobs.")
            return
        lines = ["Queued and running jobs:"]
        for job in jobs:
            lines.append(
                f"#{job.id} {job.state} | {job.workflow_name} | seed={job.seed or '-'} "
                f"| priority={job.priority}"
            )
        await update.message.reply_text("\n".join(lines))

    async def history_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/history", telegram_user=telegram_user)
        jobs = await sync_to_async(
            lambda: list(
                telegram_user.generation_jobs.filter(
                    state__in=[
                        GenerationJob.STATE_COMPLETED,
                        GenerationJob.STATE_FAILED,
                        GenerationJob.STATE_CANCELLED,
                    ]
                )
                .order_by("-created_at")[:5]
            ),
            thread_sensitive=True,
        )()
        if not jobs:
            await update.message.reply_text("No completed, failed, or cancelled jobs found.")
            return
        lines = ["Recent jobs:"]
        for job in jobs:
            lines.append(self._format_history_line(job))
        await update.message.reply_text("\n".join(lines))

    async def rerun_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/rerun",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        job = await sync_to_async(self.job_service.get_rerunnable_job, thread_sensitive=True)(
            telegram_user,
            context.args or [],
        )
        if job is None:
            await update.message.reply_text("No eligible job found to rerun.")
            return
        rerun_error = await sync_to_async(self.job_service.get_rerun_ineligibility_reason, thread_sensitive=True)(job)
        if rerun_error is not None:
            await update.message.reply_text(rerun_error)
            return
        rerun_job = await sync_to_async(self.job_service.create_rerun_job, thread_sensitive=True)(job)
        await sync_to_async(self.job_service.log_job_event, thread_sensitive=True)(
            rerun_job,
            "job_created",
            "rerun_queued",
            {"job_id": rerun_job.id, "rerun_of_job_id": job.id},
        )
        await update.message.reply_text(self._format_rerun_success_message(rerun_job, job))

    async def last_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/last", telegram_user=telegram_user)
        media_asset = await sync_to_async(
            self.oddesy_agent_service.get_latest_generated_media,
            thread_sensitive=True,
        )(telegram_user)
        if media_asset is None:
            await update.message.reply_text("No generated media found.")
            return
        with media_asset.file.open("rb") as handle:
            if media_asset.asset_type == MediaAsset.TYPE_GENERATED_VIDEO:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=handle,
                    caption=f"Latest generated media: #{media_asset.id}",
                )
            else:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=handle,
                    caption=f"Latest generated media: #{media_asset.id}",
                )

    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/cancel", telegram_user=telegram_user)
        job = await sync_to_async(self.job_service.get_latest_cancellable_job, thread_sensitive=True)(telegram_user)
        if job is None:
            await update.message.reply_text("No queued or running job found.")
            return
        cancellation_error = await sync_to_async(
            self.job_service.get_cancellation_ineligibility_reason,
            thread_sensitive=True,
        )(job)
        if cancellation_error is not None:
            await update.message.reply_text(cancellation_error)
            return
        event_type, message = await sync_to_async(self.job_service.cancel_job, thread_sensitive=True)(job)
        await sync_to_async(self.job_service.log_job_event, thread_sensitive=True)(
            job,
            "job_transition",
            event_type,
            {"job_id": job.id},
        )
        await update.message.reply_text(message)

    async def photo_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return

        photo = update.message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)
        content = await telegram_file.download_as_bytearray()
        asset = await sync_to_async(self._save_incoming_photo_asset, thread_sensitive=True)(
            telegram_user,
            photo.file_unique_id,
            photo.file_id,
            update.message.message_id,
            bytes(content),
        )

        await self.log_event(
            "media_received",
            "incoming_image_saved",
            telegram_user=telegram_user,
            metadata={"asset_id": asset.id},
        )
        await update.message.reply_text("Image saved. Send 'make video' to queue a generation job.")

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return

        text = (update.message.text or "").strip()
        await self.log_event(
            "telegram_command",
            "text_message",
            telegram_user=telegram_user,
            metadata={"text": text},
        )
        parsed_intent = await sync_to_async(self.instruction_parser.parse_text, thread_sensitive=True)(text)
        await self.log_event(
            "instruction_parsed",
            parsed_intent.action,
            telegram_user=telegram_user,
            metadata=parsed_intent.metadata or {},
        )

        if parsed_intent.action == "status":
            await self._reply_with_status(update, telegram_user)
            return
        if parsed_intent.action == "queue":
            await self._reply_with_queue(update, telegram_user)
            return
        if parsed_intent.action == "rerun":
            await self._reply_with_rerun(update, telegram_user, parsed_intent)
            return
        if parsed_intent.action != "create_job":
            await update.message.reply_text(parsed_intent.message)
            return

        last_image = await sync_to_async(
            self.oddesy_agent_service.get_latest_input_media,
            thread_sensitive=True,
        )(telegram_user)
        if last_image is None:
            await update.message.reply_text("No image found. Send an image first.")
            return

        await self._queue_job_from_media(update, context, telegram_user, last_image, parsed_intent)

    async def reject_message(self, update: Update) -> None:
        if update.message is not None:
            await update.message.reply_text("Access denied.")

    def _save_incoming_photo_asset(
        self,
        telegram_user: TelegramUser,
        file_unique_id: str,
        file_id: str,
        telegram_message_id: int,
        content: bytes,
    ) -> MediaAsset:
        asset = MediaAsset(
            telegram_user=telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name=f"{file_unique_id}.jpg",
            telegram_file_id=file_id,
            metadata={"telegram_message_id": telegram_message_id},
        )
        asset.file.save(asset.original_file_name, ContentFile(content), save=False)
        asset.save()
        return asset

    async def _queue_job_from_media(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        telegram_user: TelegramUser,
        media_asset: MediaAsset,
        parsed_intent: ParsedIntent,
    ) -> None:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        job = await sync_to_async(self.oddesy_agent_service.create_job_from_existing_media, thread_sensitive=True)(
            telegram_user=telegram_user,
            media_asset=media_asset,
            workflow_name=parsed_intent.workflow_name or settings.DEFAULT_WORKFLOW_NAME,
            prompt=parsed_intent.prompt or "make video",
            seed=parsed_intent.seed,
            metadata=parsed_intent.metadata,
        )
        await sync_to_async(self.job_service.log_job_event, thread_sensitive=True)(
            job,
            "job_created",
            "queued",
            {"job_id": job.id, "input_media_id": media_asset.id, "parser": parsed_intent.metadata.get("parser")},
        )
        await update.message.reply_text(f"Queued job #{job.id}.")

    async def _reply_with_status(self, update: Update, telegram_user: TelegramUser) -> None:
        job = await sync_to_async(
            lambda: telegram_user.generation_jobs.order_by("-created_at").first(),
            thread_sensitive=True,
        )()
        if job is None:
            await update.message.reply_text("No jobs found.")
            return
        await update.message.reply_text(self._format_status_message(job))

    async def _reply_with_queue(self, update: Update, telegram_user: TelegramUser) -> None:
        jobs = await sync_to_async(
            lambda: list(
                telegram_user.generation_jobs.filter(
                    state__in=[
                        GenerationJob.STATE_QUEUED,
                        GenerationJob.STATE_RUNNING,
                        GenerationJob.STATE_CANCELLATION_REQUESTED,
                    ]
                )
                .order_by("created_at")[:10]
            ),
            thread_sensitive=True,
        )()
        if not jobs:
            await update.message.reply_text("No queued or running jobs.")
            return
        lines = ["Queued and running jobs:"]
        for job in jobs:
            lines.append(
                f"#{job.id} {job.state} | {job.workflow_name} | seed={job.seed or '-'} "
                f"| priority={job.priority}"
            )
        await update.message.reply_text("\n".join(lines))

    async def _reply_with_rerun(
        self,
        update: Update,
        telegram_user: TelegramUser,
        parsed_intent: ParsedIntent,
    ) -> None:
        args = [str(parsed_intent.job_id)] if parsed_intent.job_id is not None else []
        job = await sync_to_async(self.job_service.get_rerunnable_job, thread_sensitive=True)(telegram_user, args)
        if job is None:
            await update.message.reply_text("No eligible job found to rerun.")
            return
        rerun_error = await sync_to_async(self.job_service.get_rerun_ineligibility_reason, thread_sensitive=True)(job)
        if rerun_error is not None:
            await update.message.reply_text(rerun_error)
            return
        rerun_job = await sync_to_async(self.job_service.create_rerun_job, thread_sensitive=True)(job)
        await sync_to_async(self.job_service.log_job_event, thread_sensitive=True)(
            rerun_job,
            "job_created",
            "rerun_queued",
            {"job_id": rerun_job.id, "rerun_of_job_id": job.id, "parser": parsed_intent.metadata.get("parser")},
        )
        await update.message.reply_text(self._format_rerun_success_message(rerun_job, job))

    def _format_rerun_success_message(self, rerun_job: GenerationJob, source_job: GenerationJob) -> str:
        suffix = ""
        if source_job.state == GenerationJob.STATE_FAILED:
            failure_type = source_job.metadata.get("failure", {}).get("failure_type", "unknown")
            suffix = f" Retry-safe failure: {failure_type}."
        elif source_job.state == GenerationJob.STATE_CANCELLED:
            suffix = " Source job was cancelled."
        return f"Queued rerun job #{rerun_job.id} from job #{source_job.id}.{suffix}"

    def _format_status_message(self, job: GenerationJob) -> str:
        lines = [
            f"Job #{job.id}: {job.state}",
            f"Workflow: {job.workflow_name}",
            f"Seed: {job.seed or '-'}",
        ]
        if job.prompt:
            lines.append(f"Prompt: {self._truncate(job.prompt, 80)}")

        output_summary = job.metadata.get("output_summary", {})
        if output_summary:
            lines.append(
                "Output: "
                f"{output_summary.get('asset_type', '-')} "
                f"| size={output_summary.get('file_size_bytes', '-')} "
                f"| duration={output_summary.get('duration_seconds', '-')}"
            )

        failure = job.metadata.get("failure", {})
        if failure:
            lines.append(
                "Failure: "
                f"{failure.get('failure_type', 'unknown')} "
                f"| retry_safe={failure.get('retry_safe', False)}"
            )
        return "\n".join(lines)

    def _format_history_line(self, job: GenerationJob) -> str:
        summary = f"#{job.id} {job.state} | {job.workflow_name} | seed={job.seed or '-'}"
        summary += f" | priority={job.priority}"
        output_summary = job.metadata.get("output_summary", {})
        if output_summary:
            return (
                f"{summary} | output={output_summary.get('asset_type', '-')} "
                f"| size={output_summary.get('file_size_bytes', '-')} "
                f"| duration={output_summary.get('duration_seconds', '-')}"
            )

        failure = job.metadata.get("failure", {})
        if failure:
            return (
                f"{summary} | failure={failure.get('failure_type', 'unknown')} "
                f"| retry_safe={failure.get('retry_safe', False)}"
            )

        if job.prompt:
            return f"{summary} | prompt={self._truncate(job.prompt, 60)}"
        return summary

    def _truncate(self, value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."
