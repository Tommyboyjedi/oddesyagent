from __future__ import annotations

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from apps.core.models import AuditLog, GenerationJob, MediaAsset, TelegramUser
from apps.core.services.workflow_manager import WorkflowManager


class Command(BaseCommand):
    help = "Run the Telegram bot that accepts images and queues generation jobs."

    def handle(self, *args, **options) -> None:
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN is not configured")

        application = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("status", self.status_command))
        application.add_handler(CommandHandler("workflows", self.workflows_command))
        application.add_handler(CommandHandler("last", self.last_command))
        application.add_handler(CommandHandler("cancel", self.cancel_command))
        application.add_handler(MessageHandler(filters.PHOTO, self.photo_message))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))

        self.stdout.write(self.style.SUCCESS("Telegram bot started"))
        application.run_polling()

    def get_or_reject_user(self, update: Update) -> TelegramUser | None:
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

    def log_event(
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
        telegram_user = self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        self.log_event("telegram_command", "/start", telegram_user=telegram_user)
        await update.message.reply_text("OddesyAgent is ready. Send an image, then send 'make video'.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        self.log_event("telegram_command", "/help", telegram_user=telegram_user)
        await update.message.reply_text(
            "/start\n/help\n/status\n/workflows\n/last\n/cancel\n\nSend an image, then send 'make video'."
        )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        self.log_event("telegram_command", "/status", telegram_user=telegram_user)
        job = telegram_user.generation_jobs.order_by("-created_at").first()
        if job is None:
            await update.message.reply_text("No jobs found.")
            return
        await update.message.reply_text(f"Job #{job.id}: {job.state}")

    async def workflows_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        self.log_event("telegram_command", "/workflows", telegram_user=telegram_user)
        workflows = WorkflowManager().list_workflows()
        await update.message.reply_text("\n".join(workflows) if workflows else "No workflows found.")

    async def last_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        self.log_event("telegram_command", "/last", telegram_user=telegram_user)
        media_asset = telegram_user.media_assets.filter(
            asset_type__in=[MediaAsset.TYPE_GENERATED_VIDEO, MediaAsset.TYPE_GENERATED_IMAGE]
        ).order_by("-created_at").first()
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
        telegram_user = self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        self.log_event("telegram_command", "/cancel", telegram_user=telegram_user)
        job = telegram_user.generation_jobs.filter(
            state__in=[GenerationJob.STATE_QUEUED, GenerationJob.STATE_RUNNING]
        ).order_by("-created_at").first()
        if job is None:
            await update.message.reply_text("No queued or running job found.")
            return
        job.mark_cancelled()
        self.log_event(
            "job_transition",
            "cancelled",
            telegram_user=telegram_user,
            generation_job=job,
            metadata={"job_id": job.id},
        )
        await update.message.reply_text(f"Cancelled job #{job.id}.")

    async def photo_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return

        photo = update.message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)
        content = await telegram_file.download_as_bytearray()
        asset = MediaAsset(
            telegram_user=telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name=f"{photo.file_unique_id}.jpg",
            telegram_file_id=photo.file_id,
            metadata={"telegram_message_id": update.message.message_id},
        )
        asset.file.save(asset.original_file_name, ContentFile(bytes(content)), save=False)
        asset.save()

        self.log_event(
            "media_received",
            "incoming_image_saved",
            telegram_user=telegram_user,
            metadata={"asset_id": asset.id},
        )
        await update.message.reply_text("Image saved. Send 'make video' to queue a generation job.")

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return

        text = (update.message.text or "").strip()
        self.log_event(
            "telegram_command",
            "text_message",
            telegram_user=telegram_user,
            metadata={"text": text},
        )

        if text.lower() != "make video":
            await update.message.reply_text("Unknown input. Send an image, then send 'make video'.")
            return

        last_image = telegram_user.media_assets.filter(
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE
        ).order_by("-created_at").first()
        if last_image is None:
            await update.message.reply_text("No image found. Send an image first.")
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        job = GenerationJob.objects.create(
            telegram_user=telegram_user,
            input_media=last_image,
            workflow_name=settings.DEFAULT_WORKFLOW_NAME,
            prompt="make video",
        )
        self.log_event(
            "job_created",
            "queued",
            telegram_user=telegram_user,
            generation_job=job,
            metadata={"job_id": job.id, "input_media_id": last_image.id},
        )
        await update.message.reply_text(f"Queued job #{job.id}.")

    async def reject_message(self, update: Update) -> None:
        if update.message is not None:
            await update.message.reply_text("Access denied.")
