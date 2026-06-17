from __future__ import annotations

import contextlib
import os
import secrets
import tempfile

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.request import HTTPXRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from apps.core.models import AuditLog, GenerationJob, MediaAsset, TelegramUser
from apps.core.services.instruction_parser import InstructionParserService, ParsedIntent
from apps.core.services.job_service import JobService
from apps.core.services.mvd_audio_preview_service import MvdAudioPreviewService
from apps.core.services.music_video_director_bridge import MusicVideoDirectorBridgeService
from apps.core.services.oddesy_agent_service import OddesyAgentService


class Command(BaseCommand):
    help = "Run the Telegram bot that accepts images and queues generation jobs."
    IMAGESWAP_STEP_ORDER = (
        "sam_prompt_text",
        "positive_prompt",
        "swap_text_prompt",
        "swap_reference_media_asset_id",
        "target_media_asset_id",
        "confirm",
    )
    IMAGESWAP_META_KEYS = {"_wizard_step", "_wizard_message"}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.job_service = JobService()
        self.oddesy_agent_service = OddesyAgentService(job_service=self.job_service)
        self.instruction_parser = InstructionParserService()
        self.music_video_director_bridge = MusicVideoDirectorBridgeService(
            repo_dir=getattr(settings, "MVD_REPO_DIR", None),
            python_executable=getattr(settings, "MVD_PYTHON_EXECUTABLE", None),
        )
        self.mvd_audio_preview_service = MvdAudioPreviewService()
        self.music_video_director_approvals: dict[str, dict] = {}

    def handle(self, *args, **options) -> None:
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN is not configured")

        application = self._build_application()
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("image", self.image_command))
        application.add_handler(CommandHandler("imageswap", self.imageswap_command))
        application.add_handler(CommandHandler("video", self.video_command))
        application.add_handler(CommandHandler("setworkflow", self.setworkflow_command))
        application.add_handler(CommandHandler("activeworkflow", self.activeworkflow_command))
        application.add_handler(CommandHandler("setvideoworkflow", self.setvideoworkflow_command))
        application.add_handler(CommandHandler("activevideoworkflow", self.activevideoworkflow_command))
        application.add_handler(CommandHandler("imageloras", self.imageloras_command))
        application.add_handler(CommandHandler("videoloras", self.videoloras_command))
        application.add_handler(CommandHandler("setimagelora", self.setimagelora_command))
        application.add_handler(CommandHandler("setvideolora", self.setvideolora_command))
        application.add_handler(CommandHandler("setimagelorastrength", self.setimagelorastrength_command))
        application.add_handler(CommandHandler("setvideolorastrength", self.setvideolorastrength_command))
        application.add_handler(CommandHandler("imagepromptboxes", self.imagepromptboxes_command))
        application.add_handler(CommandHandler("videopromptboxes", self.videopromptboxes_command))
        application.add_handler(CommandHandler("setimagepromptbox", self.setimagepromptbox_command))
        application.add_handler(CommandHandler("referencephotoimageset", self.referencephotoimageset_command))
        application.add_handler(CommandHandler("faceoutfitswapimageset", self.faceoutfitswapimageset_command))
        application.add_handler(CommandHandler("setvideopromptbox", self.setvideopromptbox_command))
        application.add_handler(CommandHandler("clearimagepromptbox", self.clearimagepromptbox_command))
        application.add_handler(CommandHandler("clearvideopromptbox", self.clearvideopromptbox_command))
        application.add_handler(CommandHandler("imagemode", self.imagemode_command))
        application.add_handler(CommandHandler("batchsize", self.batchsize_command))
        application.add_handler(CommandHandler("videonegative", self.videonegative_command))
        application.add_handler(CommandHandler("videolength", self.videolength_command))
        application.add_handler(CommandHandler("status", self.status_command))
        application.add_handler(CommandHandler("workflows", self.workflows_command))
        application.add_handler(CommandHandler("queue", self.queue_command))
        application.add_handler(CommandHandler("history", self.history_command))
        application.add_handler(CommandHandler("rerun", self.rerun_command))
        application.add_handler(CommandHandler("last", self.last_command))
        application.add_handler(CommandHandler("lastimage", self.lastimage_command))
        application.add_handler(CommandHandler("videos", self.videos_command))
        application.add_handler(CommandHandler("getvideo", self.getvideo_command))
        application.add_handler(CommandHandler("getvideojob", self.getvideojob_command))
        application.add_handler(CommandHandler("lastframe", self.lastframe_command))
        application.add_handler(CommandHandler("lastframeid", self.lastframeid_command))
        application.add_handler(CommandHandler("lastframeupscale", self.lastframeupscale_command))
        application.add_handler(CommandHandler("combinevideos", self.combinevideos_command))
        application.add_handler(CommandHandler("combinevideojobs", self.combinevideojobs_command))
        application.add_handler(CommandHandler("mvdhelp", self.mvdhelp_command))
        application.add_handler(CommandHandler("mvduse", self.mvduse_command))
        application.add_handler(CommandHandler("mvdstatus", self.mvdstatus_command))
        application.add_handler(CommandHandler("mvdprojects", self.mvdprojects_command))
        application.add_handler(CommandHandler("mvdproject", self.mvdproject_command))
        application.add_handler(CommandHandler("mvdcheckaudio", self.mvdcheckaudio_command))
        application.add_handler(CommandHandler("mvdclearstaleaudio", self.mvdclearstaleaudio_command))
        application.add_handler(CommandHandler("mvdextractsegmentaudio", self.mvdextractsegmentaudio_command))
        application.add_handler(CommandHandler("mvdprepareparts", self.mvdprepareparts_command))
        application.add_handler(CommandHandler("mvddraftpartprompts", self.mvddraftpartprompts_command))
        application.add_handler(CommandHandler("mvdgeneratepartimage", self.mvdgeneratepartimage_command))
        application.add_handler(CommandHandler("mvdgenerateprojectimages", self.mvdgenerateprojectimages_command))
        application.add_handler(CommandHandler("mvdcreativebrief", self.mvdcreativebrief_command))
        application.add_handler(CommandHandler("mvdcreativebrieftext", self.mvdcreativebrieftext_command))
        application.add_handler(CommandHandler("checksegments", self.checksegments_command))
        application.add_handler(CommandHandler("checkparts", self.checkparts_command))
        application.add_handler(CommandHandler("mvdagent", self.mvdagent_command))
        application.add_handler(CommandHandler("cancel", self.cancel_command))
        application.add_handler(CallbackQueryHandler(self.imageswap_callback_query, pattern=r"^imageswap:"))
        application.add_handler(CallbackQueryHandler(self.mvd_callback_query, pattern=r"^mvd:"))
        application.add_handler(MessageHandler(filters.PHOTO, self.photo_message))
        application.add_handler(MessageHandler(filters.VIDEO, self.video_message))
        application.add_handler(MessageHandler(filters.Document.VIDEO, self.video_document_message))
        application.add_handler(
            MessageHandler(
                filters.AUDIO | filters.VOICE | (filters.Document.ALL & ~filters.Document.VIDEO),
                self.music_video_director_audio_message,
            )
        )
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))

        self.stdout.write(self.style.SUCCESS("Telegram bot started"))
        application.run_polling()

    def _build_application(self) -> Application:
        request = HTTPXRequest(
            connection_pool_size=50,
            connect_timeout=10.0,
            read_timeout=120.0,
            write_timeout=120.0,
            pool_timeout=60.0,
        )
        return (
            Application.builder()
            .token(settings.TELEGRAM_BOT_TOKEN)
            .request(request)
            .get_updates_request(request)
            .build()
        )

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
            "OddesyAgent is ready. Use /image <prompt> for text-to-image, or send an image then reply with a plain-text video prompt or /video <prompt>."
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/help", telegram_user=telegram_user)
        help_sections = [
            (
                "Core commands:\n"
                "/start\n/help\n/image <prompt>\n/video <positive prompt>\n/setworkflow <name>\n/activeworkflow\n"
                "/setvideoworkflow <name>\n/activevideoworkflow\n/imageloras\n/videoloras\n"
                "/setimagelora <slot> <on|off|default> [lora] [strength]\n"
                "/setvideolora <slot> <on|off|default> [lora] [strength]\n"
                "/setimagelorastrength <slot> <strength>\n/setvideolorastrength <slot> <strength>\n"
                "/imagepromptboxes\n/videopromptboxes\n/setimagepromptbox <field_key> <text>\n"
                "/referencephotoimageset [media_id|lastupload|lastimage|clear]\n"
                "/faceoutfitswapimageset [media_id|lastimage|clear]\n"
                "/setvideopromptbox <field_key> <text>\n/clearimagepromptbox <field_key>\n"
                "/clearvideopromptbox <field_key>\n/videonegative [prompt|clear]\n/videolength [frames|clear]\n"
                "/imagemode [saved|all]\n/batchsize [count]\n/status\n/workflows\n/queue\n/history\n"
                "/rerun [job_id]\n/last\n/lastimage\n/videos\n/getvideo <video_id>\n/getvideojob <job_id>\n"
                "/lastframe [upscale_factor] [sharpen_amount]\n"
                "/lastframeid <video_id> [upscale_factor] [sharpen_amount]\n"
                "/lastframeupscale [video_id]\n/combinevideos [video_id_1] [video_id_2] [video_id_3] ...\n"
                "/combinevideojobs <job_id_1> <job_id_2> [job_id_3] ...\n/cancel"
            ),
            (
                "Text to image:\n"
                "Use /image <prompt>\n"
                "Use /setworkflow <name> to choose the active image workflow\n"
                "Use /activeworkflow to see the current active image workflow\n"
                "Use /imageloras to inspect Power LoRA slots on the active image workflow\n"
                "Use /setimagelora to override an image workflow LoRA slot\n"
                "Use /setimagelorastrength to change only an image workflow LoRA strength\n"
                "Use /imagepromptboxes to list extra text prompt boxes on the active image workflow\n"
                "Use /setimagepromptbox and /clearimagepromptbox to manage those workflow text boxes\n"
                "Use /imagemode saved or /imagemode all to choose Telegram image returns\n"
                "Use /batchsize 1-8 to choose how many image or video jobs to queue at once\n"
                "Use /lastimage to fetch the latest generated image"
            ),
            (
                "Image to image:\n"
                "Use /imageswap to start the guided image swap flow\n"
                "Imageswap steps: Sam3.1 Prompt Section (text) -> Positive (text) -> Face or Outfit Prompt (text) -> Face or Outfit Referance (image) -> Referance Photo (image) -> confirm\n"
                "On repeat runs, /imageswap reuses your last confirmed values so you can keep them or edit one field\n"
                "Upload photos when the swap flow asks for target image or reference image\n"
                "Use /referencephotoimageset to explicitly set the Referance Photo image slot for image-to-image workflows\n"
                "Use /faceoutfitswapimageset to set the Face or Outfit Referance image slot on workflows that support it\n"
            ),
            (
                "Image to video:\n"
                "Use /setvideoworkflow <name> to choose the active video workflow\n"
                "Use /videoloras to inspect Power LoRA slots on the active video workflow\n"
                "Use /setvideolora to override a video workflow LoRA slot\n"
                "Use /setvideolorastrength to change only a video workflow LoRA strength\n"
                "Use /videopromptboxes to list extra text prompt boxes on the active video workflow\n"
                "Use /setvideopromptbox and /clearvideopromptbox to manage those workflow text boxes\n"
                "Use /video <positive prompt> after sending an image, or send a plain-text prompt immediately after upload\n"
                "Use /videonegative to set the negative prompt override\n"
                "Use /videolength to set the video length in frames\n"
                "Upload a video to save it for later processing\n"
                "Use /videos to list recent uploaded or generated videos and their ids\n"
                "Use /getvideo <video_id> to retrieve a specific saved video\n"
                "Use /getvideojob <job_id> to retrieve the saved video output from a job\n"
                "Use /lastframe to extract and enhance the last frame from the latest saved video\n"
                "Use /lastframeid <video_id> to target a specific saved video\n"
                "Use /lastframeupscale [video_id] to queue a ComfyUI upscale for the last frame from the latest or chosen saved video\n"
                "Use /combinevideos with no ids to merge the two latest saved videos\n"
                "Use /combinevideos <video_id_1> <video_id_2> [video_id_3] ... to merge specific saved videos in order\n"
                "Use /combinevideojobs <job_id_1> <job_id_2> [job_id_3] ... to merge the output videos from completed jobs\n"
                "You can still send an image, then send 'make video'."
            ),
            (
                "Music Video Director:\n"
                "/mvdhelp\n/mvduse [project_id|clear]\n/mvdstatus\n/mvdprojects\n/mvdproject [project_id]\n"
                "/mvdcheckaudio [project_id]\n/mvdclearstaleaudio [project_id]\n"
                "/mvdextractsegmentaudio [project_id] [--segment <n>] [--replace]\n"
                "/mvdprepareparts [project_id] [--segment <n>] [--replace] [--target-seconds <n>] [--alignment <mode>]\n"
                "/mvddraftpartprompts [project_id] [--overwrite] [--model <wan2.2|ltx2|s2v>]\n"
                "/mvdgeneratepartimage [project_id] <part_id> [--tool <chatgpt>]\n"
                "/mvdgenerateprojectimages [project_id] [--limit <n>] [--tool <chatgpt>]\n"
                "/mvdcreativebrief [project_id]\n/mvdcreativebrieftext <synopsis>\n/mvdagent <instruction>\n\n"
                "Use /mvduse <project_id> to set a working music-video project.\n"
                "Send an audio file with caption 'mvd <project_id> [vocals|instrumental]' or set /mvduse first and upload audio without a caption."
            ),
        ]
        await self._reply_in_chunks(update, help_sections)

    async def image_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        prompt = " ".join(context.args or []).strip()
        await self.log_event(
            "telegram_command",
            "/image",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        if not prompt:
            await update.message.reply_text("Usage: /image <prompt>")
            return
        active_workflow = await sync_to_async(
            self.oddesy_agent_service.get_active_text_workflow,
            thread_sensitive=True,
        )(telegram_user)

        parsed_intent = ParsedIntent(
            action="create_job",
            workflow_name=active_workflow,
            prompt=prompt,
            message="create_job",
            metadata={
                "parser": "telegram_command",
                "parsed_instruction": {
                    "workflow": active_workflow,
                    "prompt": prompt,
                    "seed": 0,
                    "duration": None,
                    "motion": None,
                    "raw_text": f"/image {prompt}",
                },
            },
        )
        requires_input_media = await sync_to_async(
            self.oddesy_agent_service.workflow_requires_input_media,
            thread_sensitive=True,
        )(active_workflow)
        if requires_input_media:
            source_image = await sync_to_async(
                self._get_explicit_image_workflow_source_media,
                thread_sensitive=True,
            )(telegram_user, active_workflow)
            if source_image is None:
                await update.message.reply_text(
                    "That image workflow needs an explicit source image. "
                    "Upload an image, then use /referencephotoimageset lastupload, or set a specific image with /referencephotoimageset <media_id>."
                )
                return
            await self._queue_job_from_media(update, context, telegram_user, source_image, parsed_intent)
            return
        await self._queue_prompt_only_job(update, context, telegram_user, parsed_intent)

    async def video_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        prompt = " ".join(context.args or []).strip()
        await self.log_event(
            "telegram_command",
            "/video",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        if not prompt:
            await update.message.reply_text("Usage: /video <positive prompt>")
            return
        active_workflow = await sync_to_async(
            self.oddesy_agent_service.get_active_video_workflow,
            thread_sensitive=True,
        )(telegram_user)
        negative_prompt = await sync_to_async(
            self.oddesy_agent_service.get_active_video_negative_prompt,
            thread_sensitive=True,
        )(telegram_user)
        length_frames = await sync_to_async(
            self.oddesy_agent_service.get_active_video_length_frames,
            thread_sensitive=True,
        )(telegram_user)
        parsed_intent = ParsedIntent(
            action="create_job",
            workflow_name=active_workflow,
            prompt=prompt,
            negative_prompt=negative_prompt or None,
            length_frames=length_frames,
            message="create_job",
            metadata={
                "parser": "telegram_command",
                "parsed_instruction": {
                    "workflow": active_workflow,
                    "prompt": prompt,
                    "negative_prompt": negative_prompt or None,
                    "seed": 0,
                    "length_frames": length_frames,
                    "duration": None,
                    "motion": None,
                    "raw_text": f"/video {prompt}",
                },
            },
        )
        requires_input_media = await sync_to_async(
            self.oddesy_agent_service.workflow_requires_input_media,
            thread_sensitive=True,
        )(active_workflow)
        if requires_input_media:
            source_media, used_pending_video_media = await sync_to_async(
                self._get_video_source_media,
                thread_sensitive=True,
            )(telegram_user)
            if source_media is None:
                await update.message.reply_text("That workflow needs an input image. Send an image first.")
                return
            await self._queue_job_from_media(update, context, telegram_user, source_media, parsed_intent)
            if used_pending_video_media:
                await sync_to_async(
                    self.oddesy_agent_service.clear_pending_video_media,
                    thread_sensitive=True,
                )(telegram_user)
            return
        await self._queue_prompt_only_job(update, context, telegram_user, parsed_intent)

    async def imageswap_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/imageswap", telegram_user=telegram_user)
        await sync_to_async(self._start_or_resume_imageswap, thread_sensitive=True)(telegram_user)
        await self._reply_with_imageswap_step(update, telegram_user)

    async def imageswap_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await query.edit_message_text("Access denied.")
            return

        data = str(query.data or "")
        if data == "imageswap:keep":
            next_step = await sync_to_async(self._advance_imageswap_step, thread_sensitive=True)(telegram_user)
            if next_step == "confirm":
                await self._reply_with_imageswap_confirm(update, telegram_user, use_callback=True)
                return
            await self._reply_with_imageswap_step(update, telegram_user, use_callback=True)
            return
        if data == "imageswap:replace":
            await self._reply_with_imageswap_step(update, telegram_user, use_callback=True, replacing=True)
            return
        if data == "imageswap:editmenu":
            await self._reply_with_imageswap_edit_menu(update)
            return
        if data.startswith("imageswap:edit:"):
            step = data.split(":", 2)[2]
            await sync_to_async(self._set_imageswap_step, thread_sensitive=True)(telegram_user, step)
            await self._reply_with_imageswap_step(update, telegram_user, use_callback=True)
            return
        if data == "imageswap:repeat":
            await sync_to_async(self._load_imageswap_defaults_into_draft, thread_sensitive=True)(telegram_user)
            await self._confirm_imageswap(update, context, telegram_user, use_callback=True)
            return
        if data == "imageswap:confirm":
            await self._confirm_imageswap(update, context, telegram_user, use_callback=True)
            return
        if data == "imageswap:cancel":
            await sync_to_async(self._clear_imageswap_draft, thread_sensitive=True)(telegram_user)
            await query.edit_message_text("Imageswap draft cleared.")
            return
        await query.edit_message_text("Unknown imageswap action.")

    def _start_or_resume_imageswap(self, telegram_user: TelegramUser) -> None:
        defaults = dict(self.oddesy_agent_service.get_imageswap_defaults(telegram_user))
        draft = dict(telegram_user.imageswap_draft or {})
        if not draft:
            draft.update(defaults)
            if not draft.get("workflow_name"):
                draft["workflow_name"] = self.oddesy_agent_service.get_active_text_workflow(telegram_user)
        draft["_wizard_step"] = draft.get("_wizard_step") or self._find_first_missing_imageswap_step(draft)
        telegram_user.imageswap_draft = draft
        telegram_user.save(update_fields=["imageswap_draft", "updated_at"])

    def _find_first_missing_imageswap_step(self, draft: dict) -> str:
        for step in self.IMAGESWAP_STEP_ORDER:
            if step == "confirm":
                return "confirm"
            if draft.get(step) in (None, ""):
                return step
        return "confirm"

    def _set_imageswap_step(self, telegram_user: TelegramUser, step: str) -> None:
        draft = dict(telegram_user.imageswap_draft or {})
        draft["_wizard_step"] = step
        telegram_user.imageswap_draft = draft
        telegram_user.save(update_fields=["imageswap_draft", "updated_at"])

    def _advance_imageswap_step(self, telegram_user: TelegramUser) -> str:
        draft = dict(telegram_user.imageswap_draft or {})
        current_step = str(draft.get("_wizard_step") or self._find_first_missing_imageswap_step(draft))
        if current_step not in self.IMAGESWAP_STEP_ORDER:
            next_step = self._find_first_missing_imageswap_step(draft)
        else:
            current_index = self.IMAGESWAP_STEP_ORDER.index(current_step)
            next_step = "confirm"
            for step in self.IMAGESWAP_STEP_ORDER[current_index + 1 :]:
                if step == "confirm" or draft.get(step) in (None, ""):
                    next_step = step
                    break
        draft["_wizard_step"] = next_step
        telegram_user.imageswap_draft = draft
        telegram_user.save(update_fields=["imageswap_draft", "updated_at"])
        return next_step

    def _store_imageswap_value_and_advance(self, telegram_user: TelegramUser, field: str, value) -> str:
        kwargs = {field: value}
        self.oddesy_agent_service.set_imageswap_draft(telegram_user, **kwargs)
        draft = dict(telegram_user.imageswap_draft or {})
        next_step = self._find_first_missing_imageswap_step(draft)
        draft["_wizard_step"] = next_step
        telegram_user.imageswap_draft = draft
        telegram_user.save(update_fields=["imageswap_draft", "updated_at"])
        return next_step

    def _load_imageswap_defaults_into_draft(self, telegram_user: TelegramUser) -> None:
        defaults = dict(self.oddesy_agent_service.get_imageswap_defaults(telegram_user))
        if defaults:
            defaults["_wizard_step"] = "confirm"
        telegram_user.imageswap_draft = defaults
        telegram_user.save(update_fields=["imageswap_draft", "updated_at"])

    def _clear_imageswap_draft(self, telegram_user: TelegramUser) -> None:
        self.oddesy_agent_service.clear_imageswap_draft(telegram_user)

    async def _reply_with_imageswap_step(
        self,
        update: Update,
        telegram_user: TelegramUser,
        *,
        use_callback: bool = False,
        replacing: bool = False,
    ) -> None:
        telegram_user.refresh_from_db()
        draft = dict(telegram_user.imageswap_draft or {})
        step = str(draft.get("_wizard_step") or self._find_first_missing_imageswap_step(draft))
        current_value = draft.get(step)
        label = self._imageswap_step_label(step)
        lines = [f"Image swap setup\nStep: {label}"]
        if current_value not in (None, "") and not replacing:
            lines.append(f"Current: {self._format_imageswap_value(step, current_value)}")
        if step == "target_media_asset_id":
            lines.append("Upload the target image for the swap.")
        elif step == "swap_kind":
            lines.append("Choose whether this swap is for a face or an outfit.")
        elif step == "positive_prompt":
            lines.append("Send the positive prompt text.")
        elif step == "swap_text_prompt":
            lines.append("Send the swap text prompt.")
        elif step == "swap_reference_media_asset_id":
            lines.append("Upload the swap reference image.")
        elif step == "i2i_prompt":
            lines.append("Send the i2i prompt.")
        keyboard = self._imageswap_step_keyboard(step, has_value=current_value not in (None, "") and not replacing)
        await self._send_imageswap_message(update, "\n".join(lines), keyboard, use_callback=use_callback)

    async def _reply_with_imageswap_confirm(
        self,
        update: Update,
        telegram_user: TelegramUser,
        *,
        use_callback: bool = False,
    ) -> None:
        telegram_user.refresh_from_db()
        draft = dict(telegram_user.imageswap_draft or {})
        lines = [
            "Image swap ready",
            f"Workflow: {draft.get('workflow_name', '')}",
            f"Swap kind: {draft.get('swap_kind', '')}",
            f"Positive prompt: {draft.get('positive_prompt', '')}",
            f"Swap text prompt: {draft.get('swap_text_prompt', '')}",
            f"Target image: {self._format_imageswap_value('target_media_asset_id', draft.get('target_media_asset_id'))}",
            f"Reference image: {self._format_imageswap_value('swap_reference_media_asset_id', draft.get('swap_reference_media_asset_id'))}",
            f"I2I prompt: {draft.get('i2i_prompt', '')}",
        ]
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Confirm", callback_data="imageswap:confirm")],
                [
                    InlineKeyboardButton("Edit", callback_data="imageswap:editmenu"),
                    InlineKeyboardButton("Repeat Last", callback_data="imageswap:repeat"),
                ],
                [InlineKeyboardButton("Cancel", callback_data="imageswap:cancel")],
            ]
        )
        await self._send_imageswap_message(update, "\n".join(lines), keyboard, use_callback=use_callback)

    async def _reply_with_imageswap_edit_menu(self, update: Update) -> None:
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Target Image", callback_data="imageswap:edit:target_media_asset_id")],
                [InlineKeyboardButton("Swap Kind", callback_data="imageswap:edit:swap_kind")],
                [InlineKeyboardButton("Positive Prompt", callback_data="imageswap:edit:positive_prompt")],
                [InlineKeyboardButton("Swap Text Prompt", callback_data="imageswap:edit:swap_text_prompt")],
                [InlineKeyboardButton("Reference Image", callback_data="imageswap:edit:swap_reference_media_asset_id")],
                [InlineKeyboardButton("I2I Prompt", callback_data="imageswap:edit:i2i_prompt")],
            ]
        )
        await self._send_imageswap_message(update, "Choose a field to edit.", keyboard, use_callback=True)

    async def _confirm_imageswap(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        telegram_user: TelegramUser,
        *,
        use_callback: bool = False,
    ) -> None:
        request = await sync_to_async(self.oddesy_agent_service.build_imageswap_request, thread_sensitive=True)(telegram_user)
        job_payload = request["job_payload"]
        job = await sync_to_async(
            self.oddesy_agent_service.create_job_from_existing_media,
            thread_sensitive=True,
        )(
            telegram_user=telegram_user,
            media_asset=job_payload["media_asset"],
            workflow_name=job_payload["workflow_name"],
            prompt=job_payload["prompt"],
            metadata=job_payload["metadata"],
        )
        await sync_to_async(
            self.oddesy_agent_service.set_imageswap_defaults,
            thread_sensitive=True,
        )(
            telegram_user,
            workflow_name=request["workflow_name"],
            swap_kind=request["swap_kind"],
            target_media_asset_id=request["target_media_asset_id"],
            positive_prompt=request["positive_prompt"],
            swap_text_prompt=request["swap_text_prompt"],
            swap_reference_media_asset_id=request["swap_reference_media_asset_id"],
            i2i_prompt=request["i2i_prompt"],
        )
        await sync_to_async(self.oddesy_agent_service.clear_imageswap_draft, thread_sensitive=True)(telegram_user)
        message = f"Queued job #{job.id}."
        await self._send_imageswap_message(update, message, None, use_callback=use_callback)

    async def _send_imageswap_message(
        self,
        update: Update,
        text: str,
        reply_markup: InlineKeyboardMarkup | None,
        *,
        use_callback: bool = False,
    ) -> None:
        if use_callback and update.callback_query is not None:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
            return
        if update.message is not None:
            await update.message.reply_text(text, reply_markup=reply_markup)

    def _imageswap_step_keyboard(self, step: str, *, has_value: bool) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if step == "swap_kind":
            rows.extend(
                [
                    [InlineKeyboardButton("Face", callback_data="imageswap:set:swap_kind:face")],
                    [InlineKeyboardButton("Outfit", callback_data="imageswap:set:swap_kind:outfit")],
                ]
            )
        if has_value:
            rows.append(
                [
                    InlineKeyboardButton("Keep", callback_data="imageswap:keep"),
                    InlineKeyboardButton("Replace", callback_data="imageswap:replace"),
                ]
            )
        rows.append([InlineKeyboardButton("Cancel", callback_data="imageswap:cancel")])
        return InlineKeyboardMarkup(rows)

    def _imageswap_step_label(self, step: str) -> str:
        labels = {
            "target_media_asset_id": "Target Image",
            "swap_kind": "Swap Kind",
            "positive_prompt": "Positive Prompt",
            "swap_text_prompt": "Swap Text Prompt",
            "swap_reference_media_asset_id": "Reference Image",
            "i2i_prompt": "I2I Prompt",
            "confirm": "Confirm",
        }
        return labels.get(step, step)

    def _format_imageswap_value(self, step: str, value) -> str:
        if value in (None, ""):
            return "not set"
        if step in {"target_media_asset_id", "swap_reference_media_asset_id"}:
            return f"#{value}"
        return str(value)

    async def setworkflow_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        requested_workflow = " ".join(context.args or []).strip()
        await self.log_event(
            "telegram_command",
            "/setworkflow",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        if not requested_workflow:
            await update.message.reply_text("Usage: /setworkflow <workflow name>")
            return
        try:
            resolved_workflow = await sync_to_async(
                self.oddesy_agent_service.set_active_text_workflow,
                thread_sensitive=True,
            )(telegram_user, requested_workflow)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await update.message.reply_text(f"Active image workflow set to: {resolved_workflow}")

    async def activeworkflow_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/activeworkflow", telegram_user=telegram_user)
        active_workflow = await sync_to_async(
            self.oddesy_agent_service.get_active_text_workflow,
            thread_sensitive=True,
        )(telegram_user)
        await update.message.reply_text(f"Active image workflow: {active_workflow}")

    async def setvideoworkflow_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        requested_workflow = " ".join(context.args or []).strip()
        await self.log_event(
            "telegram_command",
            "/setvideoworkflow",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        if not requested_workflow:
            await update.message.reply_text("Usage: /setvideoworkflow <workflow name>")
            return
        try:
            resolved_workflow = await sync_to_async(
                self.oddesy_agent_service.set_active_video_workflow,
                thread_sensitive=True,
            )(telegram_user, requested_workflow)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await update.message.reply_text(f"Active video workflow set to: {resolved_workflow}")

    async def activevideoworkflow_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/activevideoworkflow", telegram_user=telegram_user)
        active_workflow = await sync_to_async(
            self.oddesy_agent_service.get_active_video_workflow,
            thread_sensitive=True,
        )(telegram_user)
        await update.message.reply_text(f"Active video workflow: {active_workflow}")

    async def imageloras_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply_with_power_loras(update, "image", "/imageloras")

    async def videoloras_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply_with_power_loras(update, "video", "/videoloras")

    async def setimagelora_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_power_lora(update, context, "image", "/setimagelora")

    async def setvideolora_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_power_lora(update, context, "video", "/setvideolora")

    async def setimagelorastrength_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_power_lora_strength(update, context, "image", "/setimagelorastrength")

    async def setvideolorastrength_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_power_lora_strength(update, context, "video", "/setvideolorastrength")

    async def imagepromptboxes_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply_with_workflow_text_fields(update, "image", "/imagepromptboxes")

    async def videopromptboxes_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply_with_workflow_text_fields(update, "video", "/videopromptboxes")

    async def setimagepromptbox_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_workflow_text_field(update, context, "image", "/setimagepromptbox")

    async def referencephotoimageset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_workflow_image_field(
            update,
            context,
            "image",
            "/referencephotoimageset",
            self._resolve_reference_photo_field_key,
            "Referance Photo",
            allow_lastupload=True,
        )

    async def faceoutfitswapimageset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_workflow_image_field(
            update,
            context,
            "image",
            "/faceoutfitswapimageset",
            self._resolve_face_or_outfit_reference_field_key,
            "Face or Outfit Referance",
        )

    async def setvideopromptbox_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_workflow_text_field(update, context, "video", "/setvideopromptbox")

    async def clearimagepromptbox_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._clear_workflow_text_field(update, context, "image", "/clearimagepromptbox")

    async def clearvideopromptbox_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._clear_workflow_text_field(update, context, "video", "/clearvideopromptbox")

    async def imagemode_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        requested_mode = " ".join(context.args or []).strip()
        await self.log_event(
            "telegram_command",
            "/imagemode",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        if not requested_mode:
            mode = await sync_to_async(
                self.oddesy_agent_service.get_image_output_mode,
                thread_sensitive=True,
            )(telegram_user)
            await update.message.reply_text(f"Image output mode: {mode}")
            return
        try:
            mode = await sync_to_async(
                self.oddesy_agent_service.set_image_output_mode,
                thread_sensitive=True,
            )(telegram_user, requested_mode)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await update.message.reply_text(f"Image output mode set to: {mode}")

    async def batchsize_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        requested_count = " ".join(context.args or []).strip()
        await self.log_event(
            "telegram_command",
            "/batchsize",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        if not requested_count:
            count = await sync_to_async(
                self.oddesy_agent_service.get_generation_batch_count,
                thread_sensitive=True,
            )(telegram_user)
            await update.message.reply_text(f"Generation batch size: {count}")
            return
        try:
            count = await sync_to_async(
                self.oddesy_agent_service.set_generation_batch_count,
                thread_sensitive=True,
            )(telegram_user, requested_count)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await update.message.reply_text(f"Generation batch size set to: {count}")

    async def videonegative_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        requested_prompt = " ".join(context.args or []).strip()
        await self.log_event(
            "telegram_command",
            "/videonegative",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        if not requested_prompt:
            current = await sync_to_async(
                self.oddesy_agent_service.get_active_video_negative_prompt,
                thread_sensitive=True,
            )(telegram_user)
            if current:
                await update.message.reply_text(f"Active video negative prompt: {current}")
            else:
                await update.message.reply_text("Active video negative prompt: workflow default")
            return
        normalized = "" if requested_prompt.lower() == "clear" else requested_prompt
        current = await sync_to_async(
            self.oddesy_agent_service.set_active_video_negative_prompt,
            thread_sensitive=True,
        )(telegram_user, normalized)
        if current:
            await update.message.reply_text("Active video negative prompt updated.")
        else:
            await update.message.reply_text("Active video negative prompt cleared. Workflow default will be used.")

    async def videolength_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        requested_length = " ".join(context.args or []).strip()
        await self.log_event(
            "telegram_command",
            "/videolength",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        if not requested_length:
            current = await sync_to_async(
                self.oddesy_agent_service.get_active_video_length_frames,
                thread_sensitive=True,
            )(telegram_user)
            if current is None:
                await update.message.reply_text("Active video length: workflow default")
            else:
                await update.message.reply_text(f"Active video length: {current} frames")
            return
        value = None if requested_length.lower() == "clear" else requested_length
        try:
            current = await sync_to_async(
                self.oddesy_agent_service.set_active_video_length_frames,
                thread_sensitive=True,
            )(telegram_user, value)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        if current is None:
            await update.message.reply_text("Active video length cleared. Workflow default will be used.")
        else:
            await update.message.reply_text(f"Active video length set to: {current} frames")

    async def _reply_with_power_loras(self, update: Update, mode: str, command_name: str) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", command_name, telegram_user=telegram_user)
        workflow_name = await sync_to_async(self._get_active_workflow_for_mode, thread_sensitive=True)(telegram_user, mode)
        try:
            slots = await sync_to_async(
                self.oddesy_agent_service.get_effective_power_lora_slots,
                thread_sensitive=True,
            )(telegram_user, workflow_name)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        if not slots:
            await update.message.reply_text(f"Active {mode} workflow has no Power LoRA slots: {workflow_name}")
            return
        lines = [f"Active {mode} workflow LoRAs: {workflow_name}"]
        for slot in slots:
            status = "on" if slot["on"] else "off"
            override = " override" if slot.get("overridden") else ""
            lines.append(
                f"Slot {slot['slot']}: {status} | {slot['lora'] or '-'} | strength={slot['strength']}{override}"
            )
        await update.message.reply_text("\n".join(lines))

    async def _set_power_lora(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        mode: str,
        command_name: str,
    ) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            command_name,
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        args = list(context.args or [])
        if len(args) < 2:
            await update.message.reply_text(f"Usage: {command_name} <slot> <on|off|default> [lora] [strength]")
            return
        try:
            slot = int(args[0])
        except ValueError:
            await update.message.reply_text("LoRA slot must be a whole number.")
            return
        action = args[1].strip().lower()
        workflow_name = await sync_to_async(self._get_active_workflow_for_mode, thread_sensitive=True)(telegram_user, mode)
        try:
            if action == "default":
                await sync_to_async(
                    self.oddesy_agent_service.clear_workflow_lora_override,
                    thread_sensitive=True,
                )(telegram_user, workflow_name, slot)
                await update.message.reply_text(f"Cleared {mode} workflow LoRA slot {slot} override.")
                return
            if action not in {"on", "off"}:
                await update.message.reply_text("LoRA action must be one of: on, off, default.")
                return
            remainder = args[2:]
            strength = None
            lora_name = None
            if remainder:
                try:
                    strength = float(remainder[-1])
                    remainder = remainder[:-1]
                except ValueError:
                    strength = None
                if remainder:
                    lora_name = " ".join(remainder).strip()
            await sync_to_async(
                self.oddesy_agent_service.set_workflow_lora_override,
                thread_sensitive=True,
            )(
                telegram_user,
                workflow_name,
                slot,
                on=(action == "on"),
                lora=lora_name,
                strength=strength,
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        slots = await sync_to_async(
            self.oddesy_agent_service.get_effective_power_lora_slots,
            thread_sensitive=True,
        )(telegram_user, workflow_name)
        current = next((item for item in slots if item["slot"] == slot), None)
        if current is None:
            await update.message.reply_text(f"Updated {mode} workflow LoRA slot {slot}.")
            return
        status = "on" if current["on"] else "off"
        await update.message.reply_text(
            f"{mode.title()} workflow LoRA slot {slot}: {status} | {current['lora'] or '-'} | strength={current['strength']}"
        )

    async def _set_power_lora_strength(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        mode: str,
        command_name: str,
    ) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            command_name,
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        args = list(context.args or [])
        if len(args) != 2:
            await update.message.reply_text(f"Usage: {command_name} <slot> <strength>")
            return
        try:
            slot = int(args[0])
        except ValueError:
            await update.message.reply_text("LoRA slot must be a whole number.")
            return
        try:
            strength = float(args[1])
        except ValueError:
            await update.message.reply_text("LoRA strength must be a number.")
            return
        workflow_name = await sync_to_async(self._get_active_workflow_for_mode, thread_sensitive=True)(telegram_user, mode)
        try:
            await sync_to_async(
                self.oddesy_agent_service.set_workflow_lora_override,
                thread_sensitive=True,
            )(
                telegram_user,
                workflow_name,
                slot,
                strength=strength,
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        slots = await sync_to_async(
            self.oddesy_agent_service.get_effective_power_lora_slots,
            thread_sensitive=True,
        )(telegram_user, workflow_name)
        current = next((item for item in slots if item["slot"] == slot), None)
        if current is None:
            await update.message.reply_text(f"Updated {mode} workflow LoRA slot {slot} strength to {strength}.")
            return
        status = "on" if current["on"] else "off"
        await update.message.reply_text(
            f"{mode.title()} workflow LoRA slot {slot}: {status} | {current['lora'] or '-'} | strength={current['strength']}"
        )

    async def _reply_with_workflow_text_fields(self, update: Update, mode: str, command_name: str) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", command_name, telegram_user=telegram_user)
        workflow_name = await sync_to_async(self._get_active_workflow_for_mode, thread_sensitive=True)(telegram_user, mode)
        try:
            fields = await sync_to_async(
                self.oddesy_agent_service.get_effective_workflow_text_fields,
                thread_sensitive=True,
            )(telegram_user, workflow_name)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        if not fields:
            await update.message.reply_text(f"Active {mode} workflow has no extra text prompt boxes: {workflow_name}")
            return
        lines = [f"Active {mode} workflow text boxes: {workflow_name}"]
        for field in fields:
            override = " override" if field.get("overridden") else ""
            value = field.get("value", "")
            preview = value if len(value) <= 80 else f"{value[:77]}..."
            lines.append(f"{field['key']}: {field['label']} = {preview or '[empty]'}{override}")
        await update.message.reply_text("\n".join(lines))

    async def _set_workflow_text_field(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        mode: str,
        command_name: str,
    ) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            command_name,
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        args = list(context.args or [])
        if len(args) < 2:
            await update.message.reply_text(f"Usage: {command_name} <field_key> <text>")
            return
        field_key = args[0].strip()
        value = " ".join(args[1:]).strip()
        workflow_name = await sync_to_async(self._get_active_workflow_for_mode, thread_sensitive=True)(telegram_user, mode)
        try:
            await sync_to_async(
                self.oddesy_agent_service.set_workflow_text_override,
                thread_sensitive=True,
            )(telegram_user, workflow_name, field_key, value)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await update.message.reply_text(f"{mode.title()} workflow text box '{field_key}' updated.")

    async def _clear_workflow_text_field(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        mode: str,
        command_name: str,
    ) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            command_name,
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        args = list(context.args or [])
        if len(args) != 1:
            await update.message.reply_text(f"Usage: {command_name} <field_key>")
            return
        workflow_name = await sync_to_async(self._get_active_workflow_for_mode, thread_sensitive=True)(telegram_user, mode)
        try:
            await sync_to_async(
                self.oddesy_agent_service.clear_workflow_text_override,
                thread_sensitive=True,
            )(telegram_user, workflow_name, args[0])
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await update.message.reply_text(f"{mode.title()} workflow text box '{args[0]}' cleared.")

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
        active_workflow = await sync_to_async(
            self.oddesy_agent_service.get_active_text_workflow,
            thread_sensitive=True,
        )(telegram_user)
        active_video_workflow = await sync_to_async(
            self.oddesy_agent_service.get_active_video_workflow,
            thread_sensitive=True,
        )(telegram_user)
        if not workflows:
            await update.message.reply_text("No workflows found.")
            return
        lines = [
            f"Active image workflow: {active_workflow}",
            f"Active video workflow: {active_video_workflow}",
            "",
            "Available workflows:",
        ]
        lines.extend(workflows)
        await update.message.reply_text("\n".join(lines))

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

    async def lastimage_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/lastimage", telegram_user=telegram_user)
        media_asset = await sync_to_async(
            self.oddesy_agent_service.get_latest_generated_image,
            thread_sensitive=True,
        )(telegram_user)
        if media_asset is None:
            await update.message.reply_text("No generated image found.")
            return
        with media_asset.file.open("rb") as handle:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=handle,
                caption=f"Latest generated image: #{media_asset.id}",
            )

    async def videos_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/videos", telegram_user=telegram_user)
        videos = await sync_to_async(self.oddesy_agent_service.list_recent_videos, thread_sensitive=True)(telegram_user, 10)
        if not videos:
            await update.message.reply_text("No saved videos found.")
            return
        lines = ["Recent saved videos:"]
        for asset in videos:
            lines.append(f"#{asset.id} | {asset.asset_type} | {asset.original_file_name}")
        await update.message.reply_text("\n".join(lines))

    async def getvideo_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/getvideo",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        args = list(context.args or [])
        if len(args) != 1 or not args[0].isdigit():
            await update.message.reply_text("Usage: /getvideo <video_id>")
            return
        video_id = int(args[0])
        media_asset = await sync_to_async(
            self.oddesy_agent_service.get_video_media_by_id,
            thread_sensitive=True,
        )(telegram_user, video_id)
        if media_asset is None:
            await update.message.reply_text(f"Saved video #{video_id} was not found.")
            return
        await self._send_video_asset(update, context, media_asset, f"Video #{media_asset.id}")

    async def getvideojob_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/getvideojob",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        args = list(context.args or [])
        if len(args) != 1 or not args[0].isdigit():
            await update.message.reply_text("Usage: /getvideojob <job_id>")
            return
        job_id = int(args[0])
        job = await sync_to_async(self.oddesy_agent_service.get_job, thread_sensitive=True)(telegram_user, job_id)
        if job is None or job.output_media_id is None:
            await update.message.reply_text(f"Job #{job_id} was not found or has no saved output video.")
            return
        media_asset = await sync_to_async(
            self.oddesy_agent_service.get_video_media_by_id,
            thread_sensitive=True,
        )(telegram_user, job.output_media_id)
        if media_asset is None:
            await update.message.reply_text(f"Job #{job_id} was not found or has no saved output video.")
            return
        await self._send_video_asset(update, context, media_asset, f"Video from job #{job_id}: #{media_asset.id}")

    async def lastframe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/lastframe",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        try:
            upscale_factor, sharpen_amount = self._parse_lastframe_args(context.args or [])
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
        try:
            media_asset = await sync_to_async(
                self.oddesy_agent_service.enhance_latest_video_last_frame,
                thread_sensitive=True,
            )(
                telegram_user,
                upscale_factor=upscale_factor,
                sharpen_amount=sharpen_amount,
            )
        except Exception as exc:
            await update.message.reply_text(f"Last-frame enhancement failed: {exc}")
            return
        if media_asset is None:
            await update.message.reply_text("No saved video found.")
            return
        with media_asset.file.open("rb") as handle:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=handle,
                caption=(
                    f"Enhanced last frame: #{media_asset.id} "
                    f"(upscale={upscale_factor:g}, sharpen={sharpen_amount:g})"
                ),
            )

    async def lastframeid_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/lastframeid",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        try:
            target_video_id, upscale_factor, sharpen_amount = self._parse_lastframeid_args(context.args or [])
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
        try:
            media_asset = await sync_to_async(
                self.oddesy_agent_service.enhance_video_last_frame_by_id,
                thread_sensitive=True,
            )(
                telegram_user,
                target_video_id,
                upscale_factor=upscale_factor,
                sharpen_amount=sharpen_amount,
            )
        except Exception as exc:
            await update.message.reply_text(f"Last-frame enhancement failed: {exc}")
            return
        if media_asset is None:
            await update.message.reply_text(f"Saved video #{target_video_id} was not found.")
            return
        with media_asset.file.open("rb") as handle:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=handle,
                caption=(
                    f"Enhanced last frame: #{media_asset.id} "
                    f"(upscale={upscale_factor:g}, sharpen={sharpen_amount:g})"
                ),
            )

    async def lastframeupscale_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/lastframeupscale",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        args = list(context.args or [])
        if len(args) > 1:
            await update.message.reply_text("Usage: /lastframeupscale [video_id]")
            return
        target_video_id = None
        if args:
            if not args[0].isdigit():
                await update.message.reply_text("Video id must be a whole number.")
                return
            target_video_id = int(args[0])
        try:
            job = await sync_to_async(
                self.oddesy_agent_service.queue_last_frame_upscale_job,
                thread_sensitive=True,
            )(telegram_user, target_video_id)
        except Exception as exc:
            await update.message.reply_text(f"Last-frame upscale failed: {exc}")
            return
        if job is None:
            if target_video_id is None:
                await update.message.reply_text("No saved video found.")
            else:
                await update.message.reply_text(f"Saved video #{target_video_id} was not found.")
            return
        source_video_id = job.metadata.get("last_frame_upscale", {}).get("source_video_media_asset_id")
        if source_video_id:
            await update.message.reply_text(
                f"Queued job #{job.id} for last-frame upscale from video #{source_video_id}."
            )
        else:
            await update.message.reply_text(f"Queued job #{job.id} for last-frame upscale.")

    async def combinevideos_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/combinevideos",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        try:
            video_ids = self._parse_combinevideos_args(context.args or [])
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        if video_ids:
            await update.message.reply_text(f"Combine request received for {len(video_ids)} video ids.")
        else:
            await update.message.reply_text("Combine request received for the two latest saved videos.")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)
        try:
            if video_ids:
                media_asset = await sync_to_async(
                    self.oddesy_agent_service.combine_videos_by_ids,
                    thread_sensitive=True,
                )(telegram_user, video_ids)
            else:
                media_asset = await sync_to_async(
                    self.oddesy_agent_service.combine_latest_videos,
                    thread_sensitive=True,
                )(telegram_user, 2)
        except Exception as exc:
            await update.message.reply_text(f"Video combination failed: {exc}")
            return
        if media_asset is None:
            if video_ids:
                await update.message.reply_text("One or more requested videos were not found.")
            else:
                await update.message.reply_text("Need at least two saved videos to combine.")
            return
        await self._send_combined_video_result(update, context, media_asset)

    async def combinevideojobs_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/combinevideojobs",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        try:
            job_ids = self._parse_combinevideojobs_args(context.args or [])
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await update.message.reply_text(f"Combine request received for {len(job_ids)} job ids.")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)
        try:
            media_asset = await sync_to_async(
                self.oddesy_agent_service.combine_videos_by_job_ids,
                thread_sensitive=True,
            )(telegram_user, job_ids)
        except Exception as exc:
            await update.message.reply_text(f"Video combination failed: {exc}")
            return
        if media_asset is None:
            await update.message.reply_text("One or more requested jobs were not found or do not have a stored video output.")
            return
        await self._send_combined_video_result(update, context, media_asset)

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

    def _start_or_resume_imageswap(self, telegram_user: TelegramUser) -> None:
        draft = dict(telegram_user.imageswap_draft or {})
        if draft.get("_wizard_step") in self.IMAGESWAP_STEP_ORDER:
            return

        defaults = self._get_imageswap_defaults_for_wizard(telegram_user)
        workflow_name = defaults.get("workflow_name") or self.oddesy_agent_service.get_active_text_workflow(telegram_user)
        draft = {key: value for key, value in defaults.items() if key not in self.IMAGESWAP_META_KEYS}
        draft["workflow_name"] = workflow_name
        draft["_wizard_step"] = self.IMAGESWAP_STEP_ORDER[0]
        telegram_user.imageswap_draft = draft
        telegram_user.save(update_fields=["imageswap_draft", "updated_at"])

    def _load_imageswap_defaults_into_draft(self, telegram_user: TelegramUser) -> None:
        defaults = self._get_imageswap_defaults_for_wizard(telegram_user)
        workflow_name = defaults.get("workflow_name") or self.oddesy_agent_service.get_active_text_workflow(telegram_user)
        telegram_user.imageswap_draft = {
            **{key: value for key, value in defaults.items() if key not in self.IMAGESWAP_META_KEYS},
            "workflow_name": workflow_name,
            "_wizard_step": "confirm",
        }
        telegram_user.save(update_fields=["imageswap_draft", "updated_at"])

    def _get_imageswap_defaults_for_wizard(self, telegram_user: TelegramUser) -> dict:
        try:
            return self.oddesy_agent_service.get_imageswap_defaults(telegram_user)
        except ValueError:
            return {
                key: value
                for key, value in dict(telegram_user.imageswap_defaults or {}).items()
                if key not in self.IMAGESWAP_META_KEYS
            }

    def _clear_imageswap_draft(self, telegram_user: TelegramUser) -> None:
        telegram_user.imageswap_draft = {}
        telegram_user.save(update_fields=["imageswap_draft", "updated_at"])

    def _set_imageswap_step(self, telegram_user: TelegramUser, step: str) -> None:
        if step not in self.IMAGESWAP_STEP_ORDER:
            raise ValueError(f"Unknown imageswap step: {step}")
        draft = dict(telegram_user.imageswap_draft or {})
        draft["_wizard_step"] = step
        telegram_user.imageswap_draft = draft
        telegram_user.save(update_fields=["imageswap_draft", "updated_at"])

    def _advance_imageswap_step(self, telegram_user: TelegramUser) -> str:
        draft = dict(telegram_user.imageswap_draft or {})
        current_step = str(draft.get("_wizard_step") or self.IMAGESWAP_STEP_ORDER[0])
        try:
            current_index = self.IMAGESWAP_STEP_ORDER.index(current_step)
        except ValueError:
            current_index = 0
        next_step = self.IMAGESWAP_STEP_ORDER[min(current_index + 1, len(self.IMAGESWAP_STEP_ORDER) - 1)]
        draft["_wizard_step"] = next_step
        telegram_user.imageswap_draft = draft
        telegram_user.save(update_fields=["imageswap_draft", "updated_at"])
        return next_step

    def _store_imageswap_value_and_advance(self, telegram_user: TelegramUser, field_name: str, value: object) -> str:
        draft = dict(telegram_user.imageswap_draft or {})
        draft[field_name] = value
        telegram_user.imageswap_draft = draft
        telegram_user.save(update_fields=["imageswap_draft", "updated_at"])
        return self._advance_imageswap_step(telegram_user)

    def _get_imageswap_current_step(self, telegram_user: TelegramUser) -> str:
        draft = dict(telegram_user.imageswap_draft or {})
        current_step = str(draft.get("_wizard_step") or "")
        if not current_step:
            return ""
        if current_step not in self.IMAGESWAP_STEP_ORDER:
            return ""
        return current_step

    def _get_imageswap_display_value(self, telegram_user: TelegramUser, field_name: str) -> str:
        draft = dict(telegram_user.imageswap_draft or {})
        value = draft.get(field_name)
        if value in (None, ""):
            return "not set"
        if field_name in {"target_media_asset_id", "swap_reference_media_asset_id"}:
            media_asset = self.oddesy_agent_service.get_image_media_by_id(telegram_user, int(value))
            if media_asset is None:
                return f"#{value}"
            return f"#{media_asset.id}: {media_asset.original_file_name}"
        return str(value)

    def _imageswap_step_label(self, field_name: str) -> str:
        labels = {
            "sam_prompt_text": "Sam3.1 Prompt Section (text)",
            "positive_prompt": "Positive (text)",
            "swap_text_prompt": "Face or Outfit Prompt (text)",
            "swap_reference_media_asset_id": "Face or Outfit Referance (image)",
            "target_media_asset_id": "Referance Photo (image)",
        }
        return labels.get(field_name, field_name.replace("_", " ").title())

    def _build_imageswap_step_keyboard(self, step: str, has_value: bool) -> InlineKeyboardMarkup:
        buttons: list[list[InlineKeyboardButton]] = []
        if has_value:
            buttons.append([InlineKeyboardButton("Keep current", callback_data="imageswap:keep")])
        buttons.append([InlineKeyboardButton("Replace", callback_data="imageswap:replace")])
        buttons.append([InlineKeyboardButton("Cancel", callback_data="imageswap:cancel")])
        return InlineKeyboardMarkup(buttons)

    async def _reply_with_imageswap_step(
        self,
        update: Update,
        telegram_user: TelegramUser,
        *,
        use_callback: bool = False,
        replacing: bool = False,
        prefix_message: str | None = None,
    ) -> None:
        step = await sync_to_async(self._get_imageswap_current_step, thread_sensitive=True)(telegram_user)
        if step == "confirm":
            await self._reply_with_imageswap_confirm(update, telegram_user, use_callback=use_callback)
            return

        current_value = await sync_to_async(self._get_imageswap_display_value, thread_sensitive=True)(telegram_user, step)
        has_value = current_value != "not set"
        step_number = self.IMAGESWAP_STEP_ORDER.index(step) + 1
        label = self._imageswap_step_label(step)

        if step in {"target_media_asset_id", "swap_reference_media_asset_id"}:
            action = "Upload a photo now."
            if has_value and not replacing:
                action = "Upload a photo to replace it, or keep the current value."
        else:
            action = f"Send the {label.lower()} as plain text."
            if has_value and not replacing:
                action = f"Send a new {label.lower()} or keep the current value."

        message = (
            "Image swap setup\n"
            f"Step {step_number}/5: {label}\n"
            f"Current: {current_value}\n"
            f"{action}"
        )
        if prefix_message:
            message = f"{prefix_message}\n\n{message}"
        keyboard = self._build_imageswap_step_keyboard(step, has_value)
        await self._reply_imageswap_message(update, message, keyboard, use_callback=use_callback)

    async def _reply_with_imageswap_confirm(
        self,
        update: Update,
        telegram_user: TelegramUser,
        *,
        use_callback: bool = False,
    ) -> None:
        draft = await sync_to_async(lambda: dict(telegram_user.imageswap_draft or {}), thread_sensitive=True)()
        lines = [
            "Image swap ready",
            f"Workflow: {draft.get('workflow_name') or 'not set'}",
            f"Sam3.1 Prompt Section (text): {draft.get('sam_prompt_text') or 'not set'}",
            f"Positive (text): {draft.get('positive_prompt') or 'not set'}",
            f"Face or Outfit Prompt (text): {draft.get('swap_text_prompt') or 'not set'}",
            f"Face or Outfit Referance (image): {await sync_to_async(self._get_imageswap_display_value, thread_sensitive=True)(telegram_user, 'swap_reference_media_asset_id')}",
            f"Referance Photo (image): {await sync_to_async(self._get_imageswap_display_value, thread_sensitive=True)(telegram_user, 'target_media_asset_id')}",
        ]
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Confirm", callback_data="imageswap:confirm")],
                [InlineKeyboardButton("Repeat last", callback_data="imageswap:repeat")],
                [InlineKeyboardButton("Edit one field", callback_data="imageswap:editmenu")],
                [InlineKeyboardButton("Cancel", callback_data="imageswap:cancel")],
            ]
        )
        await self._reply_imageswap_message(update, "\n".join(lines), keyboard, use_callback=use_callback)

    async def _reply_with_imageswap_edit_menu(self, update: Update) -> None:
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Sam3.1 Prompt Section (text)", callback_data="imageswap:edit:sam_prompt_text")],
                [InlineKeyboardButton("Positive (text)", callback_data="imageswap:edit:positive_prompt")],
                [InlineKeyboardButton("Face or Outfit Prompt (text)", callback_data="imageswap:edit:swap_text_prompt")],
                [InlineKeyboardButton("Face or Outfit Referance (image)", callback_data="imageswap:edit:swap_reference_media_asset_id")],
                [InlineKeyboardButton("Referance Photo (image)", callback_data="imageswap:edit:target_media_asset_id")],
            ]
        )
        await update.callback_query.edit_message_text("Choose one field to edit.", reply_markup=keyboard)

    async def _reply_imageswap_message(
        self,
        update: Update,
        message: str,
        reply_markup: InlineKeyboardMarkup,
        *,
        use_callback: bool = False,
    ) -> None:
        if use_callback and update.callback_query is not None:
            await update.callback_query.edit_message_text(message, reply_markup=reply_markup)
            return
        await update.message.reply_text(message, reply_markup=reply_markup)

    async def _confirm_imageswap(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        telegram_user: TelegramUser,
        *,
        use_callback: bool = False,
    ) -> None:
        try:
            request = await sync_to_async(self.oddesy_agent_service.build_imageswap_request, thread_sensitive=True)(
                telegram_user
            )
        except ValueError as exc:
            await self._reply_imageswap_message(
                update,
                f"Imageswap is not ready: {exc}",
                InlineKeyboardMarkup([[InlineKeyboardButton("Edit one field", callback_data="imageswap:editmenu")]]),
                use_callback=use_callback,
            )
            return

        current_draft = dict(telegram_user.imageswap_draft or {})
        defaults_payload = {key: value for key, value in current_draft.items() if key not in self.IMAGESWAP_META_KEYS}
        await sync_to_async(self._persist_imageswap_defaults_from_draft, thread_sensitive=True)(
            telegram_user,
            defaults_payload,
        )

        job_payload = dict(request.get("job_payload") or {})
        media_asset = job_payload.get("media_asset")
        if media_asset is None:
            job = await sync_to_async(self.oddesy_agent_service.create_job_from_prompt, thread_sensitive=True)(
                telegram_user=telegram_user,
                workflow_name=str(job_payload["workflow_name"]),
                prompt=str(job_payload["prompt"]),
                metadata=job_payload.get("metadata"),
            )
        else:
            job = await sync_to_async(self.oddesy_agent_service.create_job_from_existing_media, thread_sensitive=True)(
                telegram_user=telegram_user,
                media_asset=media_asset,
                workflow_name=str(job_payload["workflow_name"]),
                prompt=str(job_payload["prompt"]),
                metadata=job_payload.get("metadata"),
            )
        if use_callback and update.callback_query is not None:
            await update.callback_query.edit_message_text(
                f"Queued imageswap job #{job.id}. Defaults saved for the next /imageswap run."
            )
            return
        await update.message.reply_text(
            f"Queued imageswap job #{job.id}. Defaults saved for the next /imageswap run."
        )

    def _persist_imageswap_defaults_from_draft(self, telegram_user: TelegramUser, defaults_payload: dict) -> None:
        telegram_user.imageswap_defaults = defaults_payload
        telegram_user.imageswap_draft = {}
        telegram_user.save(update_fields=["imageswap_defaults", "imageswap_draft", "updated_at"])

    def _parse_lastframe_args(self, args: list[str]) -> tuple[float, float]:
        if len(args) > 2:
            raise ValueError("Usage: /lastframe [upscale_factor] [sharpen_amount]")
        upscale_factor = 2.0
        sharpen_amount = 0.4
        if len(args) >= 1:
            try:
                upscale_factor = float(args[0])
            except ValueError as exc:
                raise ValueError("Upscale factor must be a number greater than 1.") from exc
        if len(args) == 2:
            try:
                sharpen_amount = float(args[1])
            except ValueError as exc:
                raise ValueError("Sharpen amount must be a non-negative number.") from exc
        if upscale_factor <= 1:
            raise ValueError("Upscale factor must be greater than 1.")
        if sharpen_amount < 0:
            raise ValueError("Sharpen amount must be non-negative.")
        return upscale_factor, sharpen_amount

    def _parse_lastframeid_args(self, args: list[str]) -> tuple[int, float, float]:
        if not args or len(args) > 3:
            raise ValueError("Usage: /lastframeid <video_id> [upscale_factor] [sharpen_amount]")
        if not args[0].isdigit():
            raise ValueError("Video id must be a whole number.")
        upscale_factor, sharpen_amount = self._parse_lastframe_args(args[1:])
        return int(args[0]), upscale_factor, sharpen_amount

    def _parse_combinevideos_args(self, args: list[str]) -> list[int]:
        if not args:
            return []
        video_ids: list[int] = []
        for arg in args:
            if not arg.isdigit():
                raise ValueError("Usage: /combinevideos [video_id_1] [video_id_2] [video_id_3] ...")
            video_ids.append(int(arg))
        if len(video_ids) < 2:
            raise ValueError("Provide at least two video ids, or no ids to use the two latest saved videos.")
        return video_ids

    def _parse_combinevideojobs_args(self, args: list[str]) -> list[int]:
        if len(args) < 2:
            raise ValueError("Usage: /combinevideojobs <job_id_1> <job_id_2> [job_id_3] ...")
        job_ids: list[int] = []
        for arg in args:
            if not arg.isdigit():
                raise ValueError("Usage: /combinevideojobs <job_id_1> <job_id_2> [job_id_3] ...")
            job_ids.append(int(arg))
        return job_ids

    async def mvdhelp_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event("telegram_command", "/mvdhelp", telegram_user=telegram_user)
        await update.message.reply_text(
            "Music Video Director\n\n"
            "/mvduse [project_id|clear]\n"
            "/mvdstatus\n"
            "/mvdprojects\n"
            "/mvdproject [project_id]\n"
            "/mvdcheckaudio [project_id]\n"
            "/mvdclearstaleaudio [project_id]\n"
            "/mvdextractsegmentaudio [project_id] [--segment <n>] [--replace]\n"
            "/mvdprepareparts [project_id] [--segment <n>] [--replace] [--target-seconds <n>] [--alignment <heuristic|local|cloud|auto>]\n"
            "/checksegments\n"
            "/checkparts <segment_number>\n"
            "/mvddraftpartprompts [project_id] [--overwrite] [--model <wan2.2|ltx2|s2v>]\n"
            "/mvdgeneratepartimage [project_id] <part_id> [--tool <chatgpt>]\n"
            "/mvdgenerateprojectimages [project_id] [--limit <n>] [--tool <chatgpt>]\n"
            "/mvdcreativebrief [project_id]\n"
            "/mvdcreativebrieftext <synopsis>\n"
            "/mvdagent <instruction>\n\n"
            "Audio upload:\n"
            "Send an audio file with caption: mvd <project_id>\n"
            "Or: mvd <project_id> vocals\n"
            "Or: mvd <project_id> instrumental\n"
            "If /mvduse is set, you can upload audio with no caption and it will attach to that project."
        )

    async def mvduse_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/mvduse",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        if not context.args:
            working_project = await sync_to_async(
                self.oddesy_agent_service.get_musicvideo_working_project,
                thread_sensitive=True,
            )(telegram_user)
            if working_project:
                await update.message.reply_text(
                    f"Music Video Director working project: {working_project}\nUse /mvduse clear to unset it."
                )
            else:
                await update.message.reply_text("No Music Video Director working project set.")
            return

        project_id = str(context.args[0]).strip()
        if project_id.lower() == "clear":
            await sync_to_async(
                self.oddesy_agent_service.clear_musicvideo_working_project,
                thread_sensitive=True,
            )(telegram_user)
            await update.message.reply_text("Music Video Director working project cleared.")
            return

        result = await sync_to_async(
            self.music_video_director_bridge.project_summary,
            thread_sensitive=True,
        )(project_id)
        if not result.get("ok"):
            await update.message.reply_text(result.get("message") or "Music Video Director request failed.")
            return
        await sync_to_async(
            self.oddesy_agent_service.set_musicvideo_working_project,
            thread_sensitive=True,
        )(telegram_user, project_id)
        await update.message.reply_text(f"Music Video Director working project set to {project_id}.")

    async def mvdstatus_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_music_video_director_simple_command(update, context, "/mvdstatus", self.music_video_director_bridge.get_status)

    async def mvdprojects_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_music_video_director_simple_command(
            update,
            context,
            "/mvdprojects",
            self.music_video_director_bridge.list_projects,
        )

    async def mvdproject_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_music_video_director_project_command(
            update,
            context,
            "/mvdproject",
            self.music_video_director_bridge.project_summary,
            usage="Usage: /mvdproject [project_id]",
        )

    async def mvdcheckaudio_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_music_video_director_project_command(
            update,
            context,
            "/mvdcheckaudio",
            self.music_video_director_bridge.check_audio_health,
            usage="Usage: /mvdcheckaudio [project_id]",
        )

    async def mvdclearstaleaudio_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/mvdclearstaleaudio",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        project_id = await sync_to_async(self._resolve_mvd_project_argument, thread_sensitive=True)(
            telegram_user,
            list(context.args or []),
            "Usage: /mvdclearstaleaudio [project_id]",
        )
        if project_id is None:
            await update.message.reply_text("Usage: /mvdclearstaleaudio [project_id]")
            return
        await self._request_music_video_director_approval(
            update,
            action_name="clear_stale_audio_refs",
            payload={"project_id": project_id},
            description=f"Clear stale audio refs for project {project_id}",
            requested_by=str(telegram_user.telegram_user_id),
        )

    async def mvdextractsegmentaudio_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/mvdextractsegmentaudio",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        try:
            params = await sync_to_async(self._parse_mvd_extract_segment_audio_args, thread_sensitive=True)(
                telegram_user,
                list(context.args or []),
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await self._request_music_video_director_approval(
            update,
            action_name="extract_segment_audio",
            payload=params,
            description=f"Extract segment audio for project {params['project_id']}",
            requested_by=str(telegram_user.telegram_user_id),
        )

    async def mvdprepareparts_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/mvdprepareparts",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        try:
            params = await sync_to_async(self._parse_mvd_prepare_parts_args, thread_sensitive=True)(
                telegram_user,
                list(context.args or []),
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await self._request_music_video_director_approval(
            update,
            action_name="prepare_project_parts",
            payload=params,
            description=f"Prepare parts for project {params['project_id']}",
            requested_by=str(telegram_user.telegram_user_id),
        )

    async def mvddraftpartprompts_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/mvddraftpartprompts",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        try:
            params = await sync_to_async(self._parse_mvd_draft_part_prompts_args, thread_sensitive=True)(
                telegram_user,
                list(context.args or []),
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await self._request_music_video_director_approval(
            update,
            action_name="draft_project_part_prompts",
            payload=params,
            description=f"Draft part prompts for project {params['project_id']}",
            requested_by=str(telegram_user.telegram_user_id),
        )

    async def mvdgeneratepartimage_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/mvdgeneratepartimage",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        try:
            params = await sync_to_async(self._parse_mvd_generate_part_image_args, thread_sensitive=True)(
                telegram_user,
                list(context.args or []),
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await self._request_music_video_director_approval(
            update,
            action_name="generate_part_image",
            payload=params,
            description=f"Generate image for part {params['part_id']} in project {params['project_id']}",
            requested_by=str(telegram_user.telegram_user_id),
        )

    async def mvdgenerateprojectimages_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/mvdgenerateprojectimages",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        try:
            params = await sync_to_async(self._parse_mvd_generate_project_images_args, thread_sensitive=True)(
                telegram_user,
                list(context.args or []),
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await self._request_music_video_director_approval(
            update,
            action_name="generate_project_images",
            payload=params,
            description=f"Generate project images for {params['project_id']}",
            requested_by=str(telegram_user.telegram_user_id),
        )

    async def mvdcreativebrief_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/mvdcreativebrief",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        project_id = await sync_to_async(self._resolve_mvd_project_argument, thread_sensitive=True)(
            telegram_user,
            list(context.args or []),
            "Usage: /mvdcreativebrief [project_id]",
        )
        if project_id is None:
            await update.message.reply_text("Usage: /mvdcreativebrief [project_id]")
            return
        await update.message.reply_text("Generating Music Video Director creative brief...")
        result = await sync_to_async(
            self.music_video_director_bridge.creative_brief_from_project,
            thread_sensitive=True,
        )(project_id)
        await update.message.reply_text(self._format_mvd_result(result))

    async def mvdcreativebrieftext_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        synopsis = " ".join(context.args or []).strip()
        await self.log_event(
            "telegram_command",
            "/mvdcreativebrieftext",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        if not synopsis:
            await update.message.reply_text("Usage: /mvdcreativebrieftext <synopsis>")
            return
        await update.message.reply_text("Generating Music Video Director creative brief...")
        result = await sync_to_async(
            self.music_video_director_bridge.creative_brief_from_text,
            thread_sensitive=True,
        )(synopsis)
        await update.message.reply_text(self._format_mvd_result(result))

    async def checksegments_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/checksegments",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        project_id = self.oddesy_agent_service.get_musicvideo_working_project(telegram_user)
        if not project_id:
            await update.message.reply_text("No Music Video Director working project set. Use /mvduse <project_id> first.")
            return
        metadata = await sync_to_async(
            self.music_video_director_bridge.get_segment_audio_preview_metadata,
            thread_sensitive=True,
        )(project_id)
        await self._send_mvd_audio_preview(update, context, metadata, output_stem=f"{project_id}_segments")

    async def checkparts_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            "/checkparts",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        if len(context.args or []) != 1:
            await update.message.reply_text("Usage: /checkparts <segment_number>")
            return
        project_id = self.oddesy_agent_service.get_musicvideo_working_project(telegram_user)
        if not project_id:
            await update.message.reply_text("No Music Video Director working project set. Use /mvduse <project_id> first.")
            return
        try:
            segment_number = int(str((context.args or [])[0]).strip())
        except (TypeError, ValueError):
            await update.message.reply_text("Usage: /checkparts <segment_number>")
            return
        metadata = await sync_to_async(
            self.music_video_director_bridge.get_part_audio_preview_metadata,
            thread_sensitive=True,
        )(project_id, segment_number)
        await self._send_mvd_audio_preview(
            update,
            context,
            metadata,
            output_stem=f"{project_id}_segment_{segment_number}_parts",
        )

    async def mvdagent_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        instruction = " ".join(context.args or []).strip()
        await self.log_event(
            "telegram_command",
            "/mvdagent",
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        if not instruction:
            await update.message.reply_text("Usage: /mvdagent <instruction>")
            return
        result = await sync_to_async(
            self.music_video_director_bridge.interpret_instruction,
            thread_sensitive=True,
        )(instruction)
        await update.message.reply_text(self._format_mvd_agent_result(result))

    async def mvd_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await query.answer("Access denied", show_alert=True)
            return
        _, decision, token = (query.data or "").split(":", 2)
        approval = self.music_video_director_approvals.pop(token, None)
        if approval is None:
            await query.answer("Approval expired or already used", show_alert=True)
            return
        if decision != "approve":
            await query.answer()
            await query.edit_message_text("Music Video Director action rejected.")
            return
        await query.answer("Running...")
        await query.edit_message_text("Running Music Video Director action...")
        result = await sync_to_async(
            self.music_video_director_bridge.execute_action,
            thread_sensitive=True,
        )(
            approval["action_name"],
            approval["payload"],
            approved=True,
            requested_by=str(telegram_user.telegram_user_id),
        )
        await query.edit_message_text(self._format_mvd_result(result))

    async def music_video_director_audio_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        message = update.effective_message
        if message is None:
            return

        parsed = await sync_to_async(self._parse_mvd_audio_caption, thread_sensitive=True)(
            telegram_user,
            message.caption or "",
        )
        if parsed is None:
            return
        project_id, stem_type = parsed

        tg_file = None
        original_filename = "audio"
        if message.audio:
            tg_file = message.audio
            original_filename = message.audio.file_name or f"audio_{message.audio.file_unique_id}.mp3"
        elif message.document:
            tg_file = message.document
            original_filename = message.document.file_name or f"audio_{message.document.file_unique_id}"
        elif message.voice:
            tg_file = message.voice
            original_filename = f"voice_{message.voice.file_unique_id}.ogg"
        if tg_file is None:
            return

        await update.message.reply_text(
            f"Downloading Music Video Director audio for project {project_id} ({stem_type})..."
        )
        telegram_file = await context.bot.get_file(tg_file.file_id)
        suffix = os.path.splitext(original_filename)[1] or ".mp3"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, prefix=f"mvd_{project_id}_") as handle:
            temp_path = handle.name
        try:
            await telegram_file.download_to_drive(temp_path)
            result = await sync_to_async(
                self.music_video_director_bridge.attach_project_audio,
                thread_sensitive=True,
            )(
                project_id,
                temp_path,
                stem_type=stem_type,
                requested_by=str(telegram_user.telegram_user_id),
            )
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        await update.message.reply_text(self._format_mvd_result(result))

    async def _run_music_video_director_simple_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        command_name: str,
        bridge_callable,
    ) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(command_name, command_name, telegram_user=telegram_user, metadata={"args": list(context.args or [])})
        result = await sync_to_async(bridge_callable, thread_sensitive=True)()
        await update.message.reply_text(self._format_mvd_result(result))

    async def _run_music_video_director_project_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        command_name: str,
        bridge_callable,
        *,
        usage: str,
    ) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(command_name, command_name, telegram_user=telegram_user, metadata={"args": list(context.args or [])})
        project_id = await sync_to_async(self._resolve_mvd_project_argument, thread_sensitive=True)(
            telegram_user,
            list(context.args or []),
            usage,
        )
        if project_id is None:
            await update.message.reply_text(usage)
            return
        result = await sync_to_async(bridge_callable, thread_sensitive=True)(project_id)
        await update.message.reply_text(self._format_mvd_result(result))

    async def _request_music_video_director_approval(
        self,
        update: Update,
        *,
        action_name: str,
        payload: dict[str, object],
        description: str,
        requested_by: str,
    ) -> None:
        token = secrets.token_urlsafe(8)
        self.music_video_director_approvals[token] = {
            "action_name": action_name,
            "payload": payload,
        }
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("Approve", callback_data=f"mvd:approve:{token}"),
                InlineKeyboardButton("Reject", callback_data=f"mvd:reject:{token}"),
            ]]
        )
        await update.message.reply_text(
            f"Music Video Director approval required:\n{description}",
            reply_markup=keyboard,
        )

    def _format_mvd_result(self, result: dict) -> str:
        if not result.get("ok"):
            return result.get("message") or "Music Video Director request failed."
        data = result.get("data")
        if isinstance(data, dict):
            projects = data.get("projects")
            if isinstance(projects, list):
                return self._format_mvd_projects(projects)
            if {"project_id", "current_step", "current_step_status"} <= set(data.keys()):
                return self._format_mvd_project(data)
            if "project_count" in data or "active_projects" in data:
                project_count = data.get("project_count", 0)
                active_projects = data.get("active_projects") or []
                projects_text = self._format_mvd_projects(active_projects)
                return f"Projects: {project_count}\n\n{projects_text}" if projects_text else f"Projects: {project_count}"
        if isinstance(data, dict) and {"title", "synopsis", "visual_style", "mood"} & set(data.keys()):
            return self._format_mvd_creative_brief(data)
        return result.get("message") or "Music Video Director request completed."

    def _format_mvd_agent_result(self, result: dict) -> str:
        if not result.get("ok"):
            return result.get("message") or "Music Video Director agent request failed."
        data = dict(result.get("data") or {})
        proposed_command = data.get("proposed_command")
        proposed_params = data.get("proposed_params") or {}
        confidence = data.get("confidence")
        clarification = data.get("clarification_question")
        if proposed_command:
            if clarification:
                return clarification
            command_text = self._format_mvd_agent_command(str(proposed_command), proposed_params)
            if confidence is not None:
                return f"Suggested Music Video Director command:\n{command_text}\nConfidence: {confidence:.0%}"
            return f"Suggested Music Video Director command:\n{command_text}"
        return result.get("message") or "Music Video Director agent request completed."

    def _format_mvd_project(self, project: dict) -> str:
        title = project.get("song_title") or project.get("project_id") or "Unknown project"
        artist = f" by {project.get('artist_name')}" if project.get("artist_name") else ""
        lines = [
            f"{project.get('project_id')} - {title}{artist}",
            f"Step: {project.get('current_step')} ({project.get('current_step_status')})",
            f"Segments: {project.get('segments')} | Parts: {project.get('parts')} | Images: {project.get('image_assets')}",
        ]
        audio_health = project.get("audio_health")
        if isinstance(audio_health, dict) and not audio_health.get("healthy", True):
            warnings = audio_health.get("warnings") or []
            lines.extend(str(warning) for warning in warnings if warning)
        return "\n".join(lines)

    def _format_mvd_projects(self, projects: list[dict]) -> str:
        if not projects:
            return "No projects found."
        return "\n\n".join(self._format_mvd_project(project) for project in projects)

    def _format_mvd_agent_command(self, proposed_command: str, proposed_params: dict) -> str:
        command_map = {
            "explain_help": "/mvdhelp",
            "get_status": "/mvdstatus",
            "list_projects": "/mvdprojects",
            "project_summary": "/mvdproject",
            "check_audio_health": "/mvdcheckaudio",
            "clear_stale_audio_refs": "/mvdclearstaleaudio",
            "extract_segment_audio": "/mvdextractsegmentaudio",
            "prepare_project_parts": "/mvdprepareparts",
            "draft_project_part_prompts": "/mvddraftpartprompts",
            "prepare_browser_image_task": "/mvdagent prepare browser image task for <project_id> <part_id>",
            "import_part_image": "/mvdagent import part image for <project_id> <part_id> <image_path>",
            "generate_part_image": "/mvdgeneratepartimage",
            "attach_project_audio": "/mvduse <project_id> then upload audio with caption 'mvd <project_id> [vocals|instrumental]'",
            "generate_project_images": "/mvdgenerateprojectimages",
        }
        base_command = command_map.get(proposed_command, f"/mvd{proposed_command.replace('_', '')}")
        if base_command.startswith("/mvdagent "):
            return base_command
        if not proposed_params:
            return base_command
        param_parts: list[str] = []
        project_id = proposed_params.get("project_id")
        part_id = proposed_params.get("part_id")
        if project_id is not None and base_command not in {"/mvdstatus", "/mvdprojects", "/mvdhelp"}:
            param_parts.append(str(project_id))
        if part_id is not None:
            param_parts.append(str(part_id))
        for key, value in proposed_params.items():
            if key in {"project_id", "part_id"}:
                continue
            if isinstance(value, bool):
                if value:
                    param_parts.append(f"--{key.replace('_', '-')}")
                continue
            param_parts.append(f"--{key.replace('_', '-')} {value}")
        suffix = " ".join(param_parts).strip()
        return f"{base_command} {suffix}".strip()

    def _format_mvd_creative_brief(self, brief: dict) -> str:
        lines = [
            f"Music Video Director Creative Brief: {brief.get('title') or 'Untitled'}",
            "",
            f"Synopsis: {brief.get('synopsis') or ''}",
            f"Visual Style: {brief.get('visual_style') or ''}",
            f"Mood: {brief.get('mood') or ''}",
            f"Colour Palette: {', '.join(brief.get('colour_palette') or [])}",
            f"Performer Notes: {brief.get('performer_notes') or ''}",
            f"Wardrobe Notes: {brief.get('wardrobe_notes') or ''}",
            f"World Rules: {brief.get('world_rules') or ''}",
            f"Recurring Motifs: {', '.join(brief.get('recurring_motifs') or [])}",
            f"Camera Language: {brief.get('camera_language') or ''}",
            f"Negative Rules: {brief.get('negative_rules') or ''}",
            f"Continuity Rules: {brief.get('continuity_rules') or ''}",
        ]
        return "\n".join(line for line in lines if line.strip())

    def _resolve_mvd_project_argument(
        self,
        telegram_user: TelegramUser,
        args: list[str],
        usage: str,
    ) -> str | None:
        normalized = self._inject_mvd_working_project(telegram_user, args)
        if not normalized:
            return None
        return str(normalized[0]).strip()

    def _inject_mvd_working_project(self, telegram_user: TelegramUser, args: list[str]) -> list[str]:
        working_project = self.oddesy_agent_service.get_musicvideo_working_project(telegram_user)
        if working_project and (not args or self._is_mvd_option_token(args[0])):
            return [working_project] + list(args)
        return list(args)

    def _normalize_mvd_args(self, args: list[str]) -> list[str]:
        normalized: list[str] = []
        for token in args:
            if self._is_mvd_option_token(token) and not token.startswith("--"):
                token = "--" + token[1:]
            normalized.append(token)
        return normalized

    def _is_mvd_option_token(self, token: str) -> bool:
        return token.startswith("--") or token.startswith("—") or token.startswith("–")

    def _parse_mvd_extract_segment_audio_args(self, telegram_user: TelegramUser, args: list[str]) -> dict[str, object]:
        normalized = self._normalize_mvd_args(self._inject_mvd_working_project(telegram_user, args))
        if not normalized:
            raise ValueError("Usage: /mvdextractsegmentaudio [project_id] [--segment <n>] [--replace]")
        params: dict[str, object] = {"project_id": normalized[0]}
        index = 1
        while index < len(normalized):
            token = normalized[index].lower()
            if token == "--replace":
                params["replace"] = True
                index += 1
                continue
            if token == "--segment":
                if index + 1 >= len(normalized):
                    raise ValueError("Missing value for --segment.")
                index += 1
                params["segment_index"] = int(normalized[index])
                index += 1
                continue
            raise ValueError(f"Unknown option: {normalized[index]}")
        return params

    def _parse_mvd_prepare_parts_args(self, telegram_user: TelegramUser, args: list[str]) -> dict[str, object]:
        normalized = self._normalize_mvd_args(self._inject_mvd_working_project(telegram_user, args))
        if not normalized:
            raise ValueError(
                "Usage: /mvdprepareparts [project_id] [--segment <n>] [--replace] [--target-seconds <n>] [--alignment <mode>]"
            )
        params: dict[str, object] = {"project_id": normalized[0]}
        index = 1
        while index < len(normalized):
            token = normalized[index].lower()
            if token == "--replace":
                params["replace"] = True
                index += 1
                continue
            if token == "--segment":
                if index + 1 >= len(normalized):
                    raise ValueError("Missing value for --segment.")
                index += 1
                params["segment_index"] = int(normalized[index])
                index += 1
                continue
            if token == "--target-seconds":
                if index + 1 >= len(normalized):
                    raise ValueError("Missing value for --target-seconds.")
                index += 1
                params["target_part_seconds"] = float(normalized[index])
                index += 1
                continue
            if token == "--alignment":
                if index + 1 >= len(normalized):
                    raise ValueError("Missing value for --alignment.")
                index += 1
                params["alignment"] = normalized[index].lower()
                index += 1
                continue
            raise ValueError(f"Unknown option: {normalized[index]}")
        return params

    def _parse_mvd_draft_part_prompts_args(self, telegram_user: TelegramUser, args: list[str]) -> dict[str, object]:
        normalized = self._normalize_mvd_args(self._inject_mvd_working_project(telegram_user, args))
        if not normalized:
            raise ValueError("Usage: /mvddraftpartprompts [project_id] [--overwrite] [--model <wan2.2|ltx2|s2v>]")
        params: dict[str, object] = {"project_id": normalized[0]}
        index = 1
        while index < len(normalized):
            token = normalized[index].lower()
            if token == "--overwrite":
                params["overwrite"] = True
                index += 1
                continue
            if token == "--model":
                if index + 1 >= len(normalized):
                    raise ValueError("Missing value for --model.")
                index += 1
                params["video_model"] = normalized[index]
                index += 1
                continue
            raise ValueError(f"Unknown option: {normalized[index]}")
        return params

    def _parse_mvd_generate_part_image_args(self, telegram_user: TelegramUser, args: list[str]) -> dict[str, object]:
        normalized = self._normalize_mvd_args(list(args))
        working_project = self.oddesy_agent_service.get_musicvideo_working_project(telegram_user)
        if working_project and len(normalized) == 1:
            try:
                int(normalized[0])
                normalized = [working_project] + normalized
            except ValueError:
                pass
        if len(normalized) < 2:
            raise ValueError("Usage: /mvdgeneratepartimage [project_id] <part_id> [--tool <chatgpt>]")
        params: dict[str, object] = {"project_id": normalized[0], "part_id": int(normalized[1])}
        index = 2
        while index < len(normalized):
            token = normalized[index].lower()
            if token == "--tool":
                if index + 1 >= len(normalized):
                    raise ValueError("Missing value for --tool.")
                index += 1
                params["tool"] = normalized[index]
                index += 1
                continue
            raise ValueError(f"Unknown option: {normalized[index]}")
        return params

    def _parse_mvd_generate_project_images_args(self, telegram_user: TelegramUser, args: list[str]) -> dict[str, object]:
        normalized = self._normalize_mvd_args(self._inject_mvd_working_project(telegram_user, args))
        if not normalized:
            raise ValueError("Usage: /mvdgenerateprojectimages [project_id] [--limit <n>] [--tool <chatgpt>]")
        params: dict[str, object] = {"project_id": normalized[0]}
        index = 1
        while index < len(normalized):
            token = normalized[index].lower()
            if token == "--limit":
                if index + 1 >= len(normalized):
                    raise ValueError("Missing value for --limit.")
                index += 1
                params["limit"] = int(normalized[index])
                index += 1
                continue
            if token == "--tool":
                if index + 1 >= len(normalized):
                    raise ValueError("Missing value for --tool.")
                index += 1
                params["tool"] = normalized[index]
                index += 1
                continue
            raise ValueError(f"Unknown option: {normalized[index]}")
        return params

    def _parse_mvd_audio_caption(self, telegram_user: TelegramUser, caption: str) -> tuple[str, str] | None:
        cleaned = (caption or "").strip()
        if cleaned.startswith("/"):
            cleaned = cleaned.split(None, 1)[-1] if " " in cleaned else ""
        tokens = cleaned.split()
        working_project = self.oddesy_agent_service.get_musicvideo_working_project(telegram_user)
        if not tokens and working_project:
            return working_project, "audio"
        if not tokens:
            return None
        if tokens[0].lower() == "mvd":
            tokens = tokens[1:]
        elif not working_project:
            return None
        if not tokens and working_project:
            return working_project, "audio"
        if not tokens:
            return None
        project_id = tokens[0]
        if project_id.lower() in {"vocals", "instrumental"} and working_project:
            project_id = working_project
            stem_word = tokens[0].lower()
        else:
            stem_word = tokens[1].lower() if len(tokens) > 1 else "audio"
        stem_type = {"vocals": "vocals", "instrumental": "instrumental"}.get(stem_word, "audio")
        return project_id, stem_type

    async def _send_mvd_audio_preview(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        metadata: dict,
        *,
        output_stem: str,
    ) -> None:
        if not metadata.get("ok"):
            await update.message.reply_text(metadata.get("message") or "Unable to build audio preview.")
            return
        payload = dict(metadata.get("data") or {})
        preview = await sync_to_async(self.mvd_audio_preview_service.build_preview, thread_sensitive=True)(
            audio_path=str(payload.get("audio_path") or ""),
            clips=list(payload.get("clips") or []),
            output_stem=output_stem,
        )
        if not preview.get("ok"):
            await update.message.reply_text(preview.get("message") or "Unable to build audio preview.")
            return
        preview_path = str(preview.get("audio_path") or "")
        caption = str(preview.get("caption") or payload.get("caption") or "").strip()
        try:
            with open(preview_path, "rb") as audio_file:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=audio_file,
                    caption=caption,
                )
        finally:
            if preview_path:
                with contextlib.suppress(OSError):
                    os.unlink(preview_path)

    async def _send_combined_video_result(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        media_asset: MediaAsset,
    ) -> None:
        caption = f"Combined video: #{media_asset.id}"
        try:
            with media_asset.file.open("rb") as handle:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=handle,
                    caption=caption,
                )
            return
        except Exception:
            pass

        try:
            with media_asset.file.open("rb") as handle:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=handle,
                    caption=caption,
                )
            return
        except Exception as exc:
            await update.message.reply_text(
                f"Combined video created as #{media_asset.id}, but Telegram delivery failed: {exc}"
            )

    async def _send_video_asset(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        media_asset: MediaAsset,
        caption: str,
    ) -> None:
        try:
            with media_asset.file.open("rb") as handle:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=handle,
                    caption=caption,
                )
            return
        except Exception:
            pass

        try:
            with media_asset.file.open("rb") as handle:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=handle,
                    caption=caption,
                )
            return
        except Exception as exc:
            await update.message.reply_text(
                f"Video #{media_asset.id} exists, but Telegram delivery failed: {exc}"
            )

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
        active_imageswap_step = await sync_to_async(self._get_imageswap_current_step, thread_sensitive=True)(telegram_user)
        if active_imageswap_step in {"target_media_asset_id", "swap_reference_media_asset_id"}:
            next_step = await sync_to_async(
                self._store_imageswap_value_and_advance,
                thread_sensitive=True,
            )(telegram_user, active_imageswap_step, asset.id)
            field_label = self._imageswap_step_label(active_imageswap_step)
            if next_step == "confirm":
                await self._reply_with_imageswap_confirm(update, telegram_user)
                return
            await self._reply_with_imageswap_step(
                update,
                telegram_user,
                prefix_message=f"Imageswap {field_label.lower()} saved as #{asset.id}: {asset.original_file_name}.",
            )
            return
        await sync_to_async(
            self.oddesy_agent_service.set_pending_video_media,
            thread_sensitive=True,
        )(telegram_user, asset)
        await update.message.reply_text(
            "Image saved. Your next plain-text prompt will use it for video, or use /video <prompt>. "
            "Image-to-image stays explicit via /referencephotoimageset."
        )

    async def video_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        video = update.message.video
        telegram_file = await context.bot.get_file(video.file_id)
        content = await telegram_file.download_as_bytearray()
        original_file_name = getattr(video, "file_name", None) or f"{video.file_unique_id}.mp4"
        asset = await sync_to_async(self._save_incoming_video_asset, thread_sensitive=True)(
            telegram_user,
            video.file_unique_id,
            video.file_id,
            update.message.message_id,
            bytes(content),
            original_file_name,
        )
        await self.log_event(
            "media_received",
            "incoming_video_saved",
            telegram_user=telegram_user,
            metadata={"asset_id": asset.id},
        )
        await update.message.reply_text(f"Video saved as #{asset.id}. Use /videos, /lastframe, or /combinevideos.")

    async def video_document_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        document = update.message.document
        telegram_file = await context.bot.get_file(document.file_id)
        content = await telegram_file.download_as_bytearray()
        original_file_name = document.file_name or f"{document.file_unique_id}.mp4"
        asset = await sync_to_async(self._save_incoming_video_asset, thread_sensitive=True)(
            telegram_user,
            document.file_unique_id,
            document.file_id,
            update.message.message_id,
            bytes(content),
            original_file_name,
        )
        await self.log_event(
            "media_received",
            "incoming_video_saved",
            telegram_user=telegram_user,
            metadata={"asset_id": asset.id},
        )
        await update.message.reply_text(f"Video saved as #{asset.id}. Use /videos, /lastframe, or /combinevideos.")

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
        active_imageswap_step = await sync_to_async(self._get_imageswap_current_step, thread_sensitive=True)(telegram_user)
        if active_imageswap_step in {"sam_prompt_text", "positive_prompt", "swap_text_prompt"} and text:
            next_step = await sync_to_async(
                self._store_imageswap_value_and_advance,
                thread_sensitive=True,
            )(telegram_user, active_imageswap_step, text)
            if next_step == "confirm":
                await self._reply_with_imageswap_confirm(update, telegram_user)
                return
            await self._reply_with_imageswap_step(update, telegram_user)
            return
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
        if parsed_intent.action == "lastframeupscale":
            try:
                job = await sync_to_async(
                    self.oddesy_agent_service.queue_last_frame_upscale_job,
                    thread_sensitive=True,
                )(telegram_user, parsed_intent.job_id)
            except Exception as exc:
                await update.message.reply_text(f"Last-frame upscale failed: {exc}")
                return
            if job is None:
                if parsed_intent.job_id is None:
                    await update.message.reply_text("No saved video found.")
                else:
                    await update.message.reply_text(f"Saved video #{parsed_intent.job_id} was not found.")
                return
            source_video_id = job.metadata.get("last_frame_upscale", {}).get("source_video_media_asset_id")
            if source_video_id:
                await update.message.reply_text(
                    f"Queued job #{job.id} for last-frame upscale from video #{source_video_id}."
                )
            else:
                await update.message.reply_text(f"Queued job #{job.id} for last-frame upscale.")
            return
        if parsed_intent.action == "rerun":
            await self._reply_with_rerun(update, telegram_user, parsed_intent)
            return
        if parsed_intent.action != "create_job":
            await update.message.reply_text(parsed_intent.message)
            return

        selected_workflow = parsed_intent.workflow_name or settings.DEFAULT_WORKFLOW_NAME
        is_video_flow = False
        pending_video_media = await sync_to_async(
            self.oddesy_agent_service.get_pending_video_media,
            thread_sensitive=True,
        )(telegram_user)
        if pending_video_media is not None and selected_workflow == settings.TEXT_TO_IMAGE_WORKFLOW_NAME:
            is_video_flow = True
            selected_workflow = await sync_to_async(
                self.oddesy_agent_service.get_active_video_workflow,
                thread_sensitive=True,
            )(telegram_user)
            parsed_intent = await sync_to_async(
                self._merge_video_defaults_into_intent,
                thread_sensitive=True,
            )(telegram_user, parsed_intent, selected_workflow)
        elif selected_workflow == settings.DEFAULT_WORKFLOW_NAME:
            is_video_flow = True
            selected_workflow = await sync_to_async(
                self.oddesy_agent_service.get_active_video_workflow,
                thread_sensitive=True,
            )(telegram_user)
            parsed_intent = await sync_to_async(
                self._merge_video_defaults_into_intent,
                thread_sensitive=True,
            )(telegram_user, parsed_intent, selected_workflow)
        requires_input_media = await sync_to_async(
            self.oddesy_agent_service.workflow_requires_input_media,
            thread_sensitive=True,
        )(selected_workflow)
        if requires_input_media:
            if is_video_flow:
                source_media, used_pending_video_media = await sync_to_async(
                    self._get_video_source_media,
                    thread_sensitive=True,
                )(telegram_user)
                if source_media is None:
                    await update.message.reply_text("That workflow needs an input image. Send an image first.")
                    return
                await self._queue_job_from_media(update, context, telegram_user, source_media, parsed_intent)
                if used_pending_video_media:
                    await sync_to_async(
                        self.oddesy_agent_service.clear_pending_video_media,
                        thread_sensitive=True,
                    )(telegram_user)
                return
            source_image = await sync_to_async(
                self._get_explicit_image_workflow_source_media,
                thread_sensitive=True,
            )(telegram_user, selected_workflow)
            if source_image is None:
                await update.message.reply_text(
                    "That image workflow needs an explicit source image. "
                    "Upload an image, then use /referencephotoimageset lastupload, or set a specific image with /referencephotoimageset <media_id>."
                )
                return
            await self._queue_job_from_media(update, context, telegram_user, source_image, parsed_intent)
            return

        await self._queue_prompt_only_job(update, context, telegram_user, parsed_intent)

    async def reject_message(self, update: Update) -> None:
        if update.message is not None:
            await update.message.reply_text("Access denied.")

    async def _reply_in_chunks(self, update: Update, sections: list[str]) -> None:
        if update.message is None:
            return
        for section in sections:
            text = (section or "").strip()
            if not text:
                continue
            await update.message.reply_text(text)

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

    def _save_incoming_video_asset(
        self,
        telegram_user: TelegramUser,
        file_unique_id: str,
        file_id: str,
        telegram_message_id: int,
        content: bytes,
        original_file_name: str,
    ) -> MediaAsset:
        asset = MediaAsset(
            telegram_user=telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_VIDEO,
            original_file_name=original_file_name,
            telegram_file_id=file_id,
            metadata={"telegram_message_id": telegram_message_id, "file_unique_id": file_unique_id},
        )
        asset.file.save(original_file_name, ContentFile(content), save=False)
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
        batch_count = await sync_to_async(
            self.oddesy_agent_service.get_generation_batch_count,
            thread_sensitive=True,
        )(telegram_user)
        jobs = await sync_to_async(self._create_jobs_from_media_batch, thread_sensitive=True)(
            telegram_user=telegram_user,
            media_asset=media_asset,
            parsed_intent=parsed_intent,
            batch_count=batch_count,
        )
        await update.message.reply_text(self._format_batch_queue_message(jobs))

    async def _queue_prompt_only_job(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        telegram_user: TelegramUser,
        parsed_intent: ParsedIntent,
    ) -> None:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        batch_count = await sync_to_async(
            self.oddesy_agent_service.get_generation_batch_count,
            thread_sensitive=True,
        )(telegram_user)
        jobs = await sync_to_async(self._create_prompt_only_jobs_batch, thread_sensitive=True)(
            telegram_user=telegram_user,
            parsed_intent=parsed_intent,
            batch_count=batch_count,
        )
        await update.message.reply_text(self._format_batch_queue_message(jobs))

    def _create_jobs_from_media_batch(
        self,
        telegram_user: TelegramUser,
        media_asset: MediaAsset,
        parsed_intent: ParsedIntent,
        batch_count: int,
    ) -> list[GenerationJob]:
        jobs: list[GenerationJob] = []
        for batch_index in range(batch_count):
            metadata = self._build_job_metadata(
                telegram_user=telegram_user,
                parsed_intent=parsed_intent,
                workflow_name=parsed_intent.workflow_name or settings.DEFAULT_WORKFLOW_NAME,
                batch_index=batch_index + 1,
                batch_count=batch_count,
                batch_mode="input_media",
            )
            job = self.oddesy_agent_service.create_job_from_existing_media(
                telegram_user=telegram_user,
                media_asset=media_asset,
                workflow_name=parsed_intent.workflow_name or settings.DEFAULT_WORKFLOW_NAME,
                prompt=parsed_intent.prompt or "make video",
                seed=parsed_intent.seed,
                metadata=metadata,
            )
            self.job_service.log_job_event(
                job,
                "job_created",
                "queued",
                {
                    "job_id": job.id,
                    "input_media_id": media_asset.id,
                    "parser": (parsed_intent.metadata or {}).get("parser"),
                    "batch_index": batch_index + 1,
                    "batch_count": batch_count,
                },
            )
            jobs.append(job)
        return jobs

    def _create_prompt_only_jobs_batch(
        self,
        telegram_user: TelegramUser,
        parsed_intent: ParsedIntent,
        batch_count: int,
    ) -> list[GenerationJob]:
        jobs: list[GenerationJob] = []
        for batch_index in range(batch_count):
            metadata = self._build_job_metadata(
                telegram_user=telegram_user,
                parsed_intent=parsed_intent,
                workflow_name=parsed_intent.workflow_name or settings.TEXT_TO_IMAGE_WORKFLOW_NAME,
                batch_index=batch_index + 1,
                batch_count=batch_count,
                batch_mode="prompt_only",
            )
            job = self.oddesy_agent_service.create_job_from_prompt(
                telegram_user=telegram_user,
                workflow_name=parsed_intent.workflow_name or settings.TEXT_TO_IMAGE_WORKFLOW_NAME,
                prompt=parsed_intent.prompt or "",
                seed=parsed_intent.seed,
                metadata=metadata,
            )
            self.job_service.log_job_event(
                job,
                "job_created",
                "queued",
                {
                    "job_id": job.id,
                    "parser": (parsed_intent.metadata or {}).get("parser"),
                    "mode": "prompt_only",
                    "batch_index": batch_index + 1,
                    "batch_count": batch_count,
                },
            )
            jobs.append(job)
        return jobs

    def _merge_video_defaults_into_intent(
        self,
        telegram_user: TelegramUser,
        parsed_intent: ParsedIntent,
        workflow_name: str,
    ) -> ParsedIntent:
        negative_prompt = parsed_intent.negative_prompt
        if negative_prompt is None:
            negative_prompt = self.oddesy_agent_service.get_active_video_negative_prompt(telegram_user) or None
        length_frames = parsed_intent.length_frames
        if length_frames is None:
            length_frames = self.oddesy_agent_service.get_active_video_length_frames(telegram_user)
        metadata = {
            **(parsed_intent.metadata or {}),
            "parsed_instruction": {
                **((parsed_intent.metadata or {}).get("parsed_instruction", {})),
                "workflow": workflow_name,
                "negative_prompt": negative_prompt,
                "length_frames": length_frames,
                "lora_overrides": self.oddesy_agent_service.get_workflow_lora_overrides(telegram_user, workflow_name),
                "text_overrides": self.oddesy_agent_service.get_workflow_text_overrides(telegram_user, workflow_name),
            },
        }
        return ParsedIntent(
            action=parsed_intent.action,
            workflow_name=workflow_name,
            prompt=parsed_intent.prompt,
            negative_prompt=negative_prompt,
            seed=parsed_intent.seed,
            duration=parsed_intent.duration,
            length_frames=length_frames,
            motion=parsed_intent.motion,
            job_id=parsed_intent.job_id,
            needs_confirmation=parsed_intent.needs_confirmation,
            message=parsed_intent.message,
            metadata=metadata,
        )

    def _build_job_metadata(
        self,
        telegram_user: TelegramUser,
        parsed_intent: ParsedIntent,
        workflow_name: str,
        batch_index: int,
        batch_count: int,
        batch_mode: str,
    ) -> dict:
        try:
            lora_overrides = self.oddesy_agent_service.get_workflow_lora_overrides(telegram_user, workflow_name)
        except ValueError:
            lora_overrides = {}
        try:
            text_overrides = self.oddesy_agent_service.get_workflow_text_overrides(telegram_user, workflow_name)
        except ValueError:
            text_overrides = {}
        try:
            image_overrides = self.oddesy_agent_service.get_workflow_image_overrides(telegram_user, workflow_name)
        except ValueError:
            image_overrides = {}
        parsed_instruction = {
            **((parsed_intent.metadata or {}).get("parsed_instruction", {})),
            "workflow": workflow_name,
            "negative_prompt": parsed_intent.negative_prompt,
            "length_frames": parsed_intent.length_frames,
            "lora_overrides": lora_overrides,
            "text_overrides": text_overrides,
            "image_overrides": image_overrides,
        }
        metadata = {
            **(parsed_intent.metadata or {}),
            "parsed_instruction": parsed_instruction,
            "batch": {
                "index": batch_index,
                "count": batch_count,
                "mode": batch_mode,
            },
        }
        return metadata

    def _get_active_workflow_for_mode(self, telegram_user: TelegramUser, mode: str) -> str:
        if mode == "video":
            return self.oddesy_agent_service.get_active_video_workflow(telegram_user)
        return self.oddesy_agent_service.get_active_text_workflow(telegram_user)

    def _resolve_face_or_outfit_reference_field_key(self, workflow_name: str) -> str | None:
        try:
            fields = self.oddesy_agent_service.list_workflow_image_fields(workflow_name)
        except ValueError:
            return None
        for field in fields:
            label = str(field.get("label", "")).lower()
            key = str(field.get("key", "")).lower()
            if "face or outfit referance" in label or "face or outfit reference" in label:
                return key
        return None

    def _resolve_reference_photo_field_key(self, workflow_name: str) -> str | None:
        try:
            fields = self.oddesy_agent_service.list_workflow_image_fields(workflow_name)
        except ValueError:
            return None
        for field in fields:
            label = str(field.get("label", "")).lower()
            key = str(field.get("key", "")).lower()
            if "referance photo" in label or "reference photo" in label:
                return key
        return None

    def _get_explicit_image_workflow_source_media(self, telegram_user: TelegramUser, workflow_name: str) -> MediaAsset | None:
        field_key = self._resolve_reference_photo_field_key(workflow_name)
        if field_key is None:
            return None
        overrides = self.oddesy_agent_service.get_workflow_image_overrides(telegram_user, workflow_name)
        media_id = overrides.get(field_key)
        if media_id is None:
            return None
        return self.oddesy_agent_service.get_image_media_by_id(telegram_user, media_id)

    def _get_video_source_media(self, telegram_user: TelegramUser) -> tuple[MediaAsset | None, bool]:
        pending_media = self.oddesy_agent_service.get_pending_video_media(telegram_user)
        if pending_media is not None:
            return pending_media, True
        latest_media = self.oddesy_agent_service.get_latest_input_media(telegram_user)
        return latest_media, False

    async def _set_workflow_image_field(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        mode: str,
        command_name: str,
        field_resolver,
        field_label: str,
        *,
        allow_lastupload: bool = False,
    ) -> None:
        telegram_user = await self.get_or_reject_user(update)
        if telegram_user is None:
            await self.reject_message(update)
            return
        await self.log_event(
            "telegram_command",
            command_name,
            telegram_user=telegram_user,
            metadata={"args": list(context.args or [])},
        )
        workflow_name = await sync_to_async(self._get_active_workflow_for_mode, thread_sensitive=True)(telegram_user, mode)
        field_key = await sync_to_async(field_resolver, thread_sensitive=True)(workflow_name)
        if field_key is None:
            await update.message.reply_text(f"Active {mode} workflow has no {field_label} image field: {workflow_name}")
            return

        args = list(context.args or [])
        if not args:
            current = await sync_to_async(
                self.oddesy_agent_service.get_workflow_image_overrides,
                thread_sensitive=True,
            )(telegram_user, workflow_name)
            media_id = current.get(field_key)
            if media_id is None:
                usage = f"Usage: {command_name} <media_id"
                if allow_lastupload:
                    usage += "|lastupload"
                usage += "|lastimage|clear>"
                await update.message.reply_text(f"{field_label} image: workflow default. {usage}")
                return
            media_asset = await sync_to_async(
                self.oddesy_agent_service.get_image_media_by_id,
                thread_sensitive=True,
            )(telegram_user, media_id)
            if media_asset is None:
                await update.message.reply_text(
                    f"{field_label} image override points to missing image #{media_id}. Use {command_name} clear to remove it."
                )
                return
            await update.message.reply_text(
                f"{field_label} image set to #{media_asset.id}: {media_asset.original_file_name or media_asset.file.name}"
            )
            return

        selector = " ".join(args).strip()
        lowered_selector = selector.lower()
        if lowered_selector == "clear":
            await sync_to_async(
                self.oddesy_agent_service.clear_workflow_image_override,
                thread_sensitive=True,
            )(telegram_user, workflow_name, field_key)
            await update.message.reply_text(f"{field_label} image cleared. Workflow default will be used.")
            return
        if lowered_selector == "lastimage":
            media_asset = await sync_to_async(
                self.oddesy_agent_service.get_latest_generated_image,
                thread_sensitive=True,
            )(telegram_user)
            if media_asset is None:
                await update.message.reply_text("No generated image found. Use /lastimage after generating one.")
                return
        elif allow_lastupload and lowered_selector == "lastupload":
            media_asset = await sync_to_async(
                self.oddesy_agent_service.get_latest_input_media,
                thread_sensitive=True,
            )(telegram_user)
            if media_asset is None:
                await update.message.reply_text("No uploaded image found. Upload an image first.")
                return
        else:
            media_asset = await sync_to_async(
                self.oddesy_agent_service.get_image_media_by_id,
                thread_sensitive=True,
            )(telegram_user, selector)
            if media_asset is None:
                usage = f"Image not found. Use {command_name} <media_id>"
                if allow_lastupload:
                    usage = f"Image not found. Use {command_name} <media_id>, {command_name} lastupload, {command_name} lastimage, or {command_name} clear."
                else:
                    usage = f"Image not found. Use {command_name} <media_id>, {command_name} lastimage, or {command_name} clear."
                await update.message.reply_text(usage)
                return

        await sync_to_async(
            self.oddesy_agent_service.set_workflow_image_override,
            thread_sensitive=True,
        )(telegram_user, workflow_name, field_key, media_asset.id)
        await update.message.reply_text(
            f"{field_label} image set to #{media_asset.id}: {media_asset.original_file_name or media_asset.file.name}"
        )

    def _format_batch_queue_message(self, jobs: list[GenerationJob]) -> str:
        if len(jobs) == 1:
            return f"Queued job #{jobs[0].id}."
        job_ids = ", ".join(f"#{job.id}" for job in jobs)
        return f"Queued {len(jobs)} jobs: {job_ids}."

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
