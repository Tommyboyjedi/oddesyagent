from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
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
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message)
        )

        self.stdout.write(self.style.SUCCESS("Telegram bot started"))
        application.run_polling()

    def get_or_reject_user(self, update: Update) -> TelegramUser | None:
        telegram_user = update.effective_user
        if telegram_user is None:
            return None
        is_allowed = telegram_user.id in settings.TELEGRAM_ALLOWED_USER_IDS
        user, _ = TelegramUser.objects.update_or_create(
            telegram_id=telegram_user.id,
            defaults={
                "username": telegram_user.username or "",
                "first_name": telegram_user.first_name or "",
                "last_name": telegram_user.last_name or "",
                "is_allowed": is_allowed,
                "last_seen_at": timezone.now(),
            },
        )
        if not is_allowed:
            AuditLog.objects.create(
                telegram_user=user,
                event_type="access_rejected",
                message="Rejected Telegram user",
                payload={"telegram_id": telegram_user.id},
            )
            return None
        return user

    def log_command(self, user: TelegramUser, command_name: str, payload: dict | None = None) -> None:
        AuditLog.objects.create(
            telegram_user=user,
            event_type="telegram_command",
            message=command_name,
            payload=payload or {},
        )

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.get_or_reject_user(update)
        if user is None:
            await self.reject_message(update)
            return
        self.log_command(user, "/start")
        await update.message.reply_text(
            "OddesyAgent is ready. Send an image, then send 'make video'."
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.get_or_reject_user(update)
        if user is None:
            await self.reject_message(update)
            return
        self.log_command(user, "/help")
        await update.message.reply_text(
            "/start\n/help\n/status\n/workflows\n/last\n/cancel\n\n"
            "MVP flow: send an image, then send 'make video'."
        )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.get_or_reject_user(update)
        if user is None:
            await self.reject_message(update)
            return
        self.log_command(user, "/status")
        last_job = user.generation_jobs.order_by("-created_at").first()
        if not last_job:
            await update.message.reply_text("No jobs found.")
            return
        await update.message.reply_text(
            f"Last job #{last_job.id}: {last_job.state}"
        )

    async def workflows_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.get_or_reject_user(update)
        if user is None:
            await self.reject_message(update)
            return
        self.log_command(user, "/workflows")
        workflows = WorkflowManager().list_workflows()
        await update.message.reply_text("\n".join(workflows) if workflows else "No workflows found.")

    async def last_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.get_or_reject_user(update)
        if user is None:
            await self.reject_message(update)
            return
        self.log_command(user, "/last")
        last_asset = user.media_assets.order_by("-created_at").first()
        if not last_asset:
            await update.message.reply_text("No media found.")
            return
        await update.message.reply_text(
            f"Last media #{last_asset.id}: {last_asset.kind} from {last_asset.source}"
        )

    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.get_or_reject_user(update)
        if user is None:
            await self.reject_message(update)
            return
        self.log_command(user, "/cancel")
        active_job = user.generation_jobs.filter(
            state__in=[GenerationJob.STATE_QUEUED, GenerationJob.STATE_RUNNING]
        ).order_by("-created_at").first()
        if not active_job:
            await update.message.reply_text("No queued or running job found.")
            return
        active_job.state = GenerationJob.STATE_CANCELLED
        active_job.save(update_fields=["state", "updated_at"])
        AuditLog.objects.create(
            telegram_user=user,
            event_type="job_transition",
            message="cancelled",
            payload={"job_id": active_job.id},
        )
        await update.message.reply_text(f"Cancelled job #{active_job.id}.")

    async def photo_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.get_or_reject_user(update)
        if user is None:
            await self.reject_message(update)
            return
        photo = update.message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)
        target_dir = Path(settings.MEDIA_ROOT) / "telegram_uploads"
        target_dir.mkdir(parents=True, exist_ok=True)
        local_path = target_dir / f"{photo.file_unique_id}.jpg"
        await telegram_file.download_to_drive(custom_path=str(local_path))

        with local_path.open("rb") as handle:
            asset = MediaAsset.objects.create(
                telegram_user=user,
                file=File(handle, name=f"telegram_uploads/{local_path.name}"),
                kind=MediaAsset.KIND_IMAGE,
                source=MediaAsset.SOURCE_TELEGRAM,
                original_name=local_path.name,
                metadata={
                    "telegram_file_id": photo.file_id,
                    "telegram_message_id": update.message.message_id,
                },
            )
        AuditLog.objects.create(
            telegram_user=user,
            event_type="media_received",
            message="telegram_image_saved",
            payload={"asset_id": asset.id},
        )
        await update.message.reply_text(
            "Image saved. Send 'make video' to queue a generation job."
        )

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.get_or_reject_user(update)
        if user is None:
            await self.reject_message(update)
            return
        text = (update.message.text or "").strip()
        self.log_command(user, "text_message", {"text": text})
        if text.lower() != "make video":
            await update.message.reply_text(
                "Unknown input. Send an image, then send 'make video'."
            )
            return

        last_image = user.media_assets.filter(kind=MediaAsset.KIND_IMAGE).order_by("-created_at").first()
        if not last_image:
            await update.message.reply_text("No image found. Send an image first.")
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        job = GenerationJob.objects.create(
            telegram_user=user,
            input_asset=last_image,
            workflow_name="i2v_wan_480p",
            prompt_text=settings.DEFAULT_PROMPT,
        )
        AuditLog.objects.create(
            telegram_user=user,
            event_type="job_transition",
            message="queued",
            payload={"job_id": job.id},
        )
        await update.message.reply_text(f"Queued job #{job.id}.")

    async def reject_message(self, update: Update) -> None:
        if update.message:
            await update.message.reply_text("Access denied.")
