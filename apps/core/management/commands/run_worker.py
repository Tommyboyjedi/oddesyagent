from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from telegram import Bot

from apps.core.models import AuditLog, GenerationJob, MediaAsset
from apps.core.services.comfyui_client import ComfyUIClient, ComfyUIClientError
from apps.core.services.workflow_manager import WorkflowManager


class Command(BaseCommand):
    help = "Run the serial job worker for ComfyUI generation."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--once", action="store_true", help="Process at most one job then exit.")
        parser.add_argument("--sleep-seconds", type=int, default=3, help="Sleep interval when no work exists.")
        parser.add_argument("--poll-seconds", type=int, default=5, help="ComfyUI polling interval.")
        parser.add_argument("--timeout-seconds", type=int, default=1800, help="Max wait for one ComfyUI job.")

    def handle(self, *args, **options) -> None:
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN) if settings.TELEGRAM_BOT_TOKEN else None
        comfy_client = ComfyUIClient()
        workflow_manager = WorkflowManager()

        processed_jobs = 0
        self.stdout.write(self.style.SUCCESS("Worker started"))
        while True:
            job = self._claim_next_job()
            if job is None:
                if options["once"]:
                    self.stdout.write("No queued jobs found.")
                    return
                time.sleep(options["sleep_seconds"])
                continue

            self._process_job(
                job=job,
                bot=bot,
                comfy_client=comfy_client,
                workflow_manager=workflow_manager,
                poll_seconds=options["poll_seconds"],
                timeout_seconds=options["timeout_seconds"],
            )
            processed_jobs += 1
            if options["once"] and processed_jobs >= 1:
                return

    def _claim_next_job(self) -> GenerationJob | None:
        with transaction.atomic():
            running_exists = GenerationJob.objects.select_for_update().filter(
                state=GenerationJob.STATE_RUNNING
            ).exists()
            if running_exists:
                return None

            job = (
                GenerationJob.objects.select_for_update()
                .filter(state=GenerationJob.STATE_QUEUED)
                .order_by("created_at")
                .first()
            )
            if job is None:
                return None

            job.mark_running()
            self._log_event(job, "job_transition", "running", {"job_id": job.id})
            return job

    def _process_job(
        self,
        job: GenerationJob,
        bot: Bot | None,
        comfy_client: ComfyUIClient,
        workflow_manager: WorkflowManager,
        poll_seconds: int,
        timeout_seconds: int,
    ) -> None:
        try:
            input_path = Path(job.input_media.file.path)
            uploaded_name = comfy_client.upload_input_image(input_path)
            if not job.seed:
                job.seed = random.randint(1, 2**31 - 1)

            workflow = workflow_manager.render_workflow(
                job.workflow_name,
                {
                    "{INPUT_IMAGE}": uploaded_name,
                    "{PROMPT}": job.prompt,
                    "{SEED}": job.seed,
                },
            )
            prompt_id = comfy_client.submit_workflow(workflow)
            job.comfyui_prompt_id = prompt_id
            job.metadata["workflow_submitted"] = True
            job.save(update_fields=["seed", "comfyui_prompt_id", "metadata", "updated_at"])

            history = comfy_client.wait_for_completion(
                prompt_id=prompt_id,
                poll_seconds=poll_seconds,
                timeout_seconds=timeout_seconds,
            )
            outputs = comfy_client.get_outputs_from_history(prompt_id)
            if not outputs:
                raise ComfyUIClientError("ComfyUI completed but returned no outputs")

            output_info = self._pick_output(outputs)
            output_asset = self._create_output_asset(job, comfy_client, output_info)
            job.metadata["comfyui_history"] = history
            job.metadata["output_info"] = output_info
            job.save(update_fields=["metadata", "updated_at"])
            job.mark_completed(output_asset)
            self._log_event(
                job,
                "job_transition",
                "completed",
                {"job_id": job.id, "output_media_id": output_asset.id},
            )
            self._log_event(
                job,
                "output_recorded",
                "generated_output_saved",
                {"job_id": job.id, "output_media_id": output_asset.id},
            )
            if bot is not None:
                self._send_result(bot, job, output_asset)
        except Exception as exc:
            job.mark_failed(str(exc))
            self._log_event(
                job,
                "job_transition",
                "failed",
                {"job_id": job.id, "error": str(exc)},
            )
            if bot is not None:
                asyncio.run(
                    bot.send_message(
                        chat_id=job.telegram_user.telegram_user_id,
                        text=f"Job #{job.id} failed: {exc}",
                    )
                )

    def _pick_output(self, outputs: list[dict]) -> dict:
        for output in outputs:
            filename = str(output.get("filename", "")).lower()
            if filename.endswith((".mp4", ".webm", ".mov")):
                return output
        return outputs[0]

    def _create_output_asset(
        self,
        job: GenerationJob,
        comfy_client: ComfyUIClient,
        output_info: dict,
    ) -> MediaAsset:
        filename = output_info.get("filename", f"job_{job.id}_output.bin")
        file_bytes = comfy_client.download_output_file(
            filename=filename,
            subfolder=output_info.get("subfolder", ""),
            output_type=output_info.get("type", "output"),
        )

        asset_type = MediaAsset.TYPE_OTHER
        suffix = Path(filename).suffix.lower()
        if suffix in {".mp4", ".webm", ".mov"}:
            asset_type = MediaAsset.TYPE_GENERATED_VIDEO
        elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            asset_type = MediaAsset.TYPE_GENERATED_IMAGE

        output_asset = MediaAsset(
            telegram_user=job.telegram_user,
            asset_type=asset_type,
            original_file_name=filename,
            metadata=output_info,
        )
        output_asset.file.save(filename, ContentFile(file_bytes), save=False)
        output_asset.save()
        return output_asset

    def _send_result(self, bot: Bot, job: GenerationJob, output_asset: MediaAsset) -> None:
        with output_asset.file.open("rb") as handle:
            if output_asset.asset_type == MediaAsset.TYPE_GENERATED_VIDEO:
                asyncio.run(
                    bot.send_video(
                        chat_id=job.telegram_user.telegram_user_id,
                        video=handle,
                        caption=f"Job #{job.id} completed.",
                    )
                )
            else:
                asyncio.run(
                    bot.send_document(
                        chat_id=job.telegram_user.telegram_user_id,
                        document=handle,
                        caption=f"Job #{job.id} completed.",
                    )
                )

    def _log_event(self, job: GenerationJob, event_type: str, message: str, metadata: dict) -> None:
        AuditLog.objects.create(
            event_type=event_type,
            telegram_user=job.telegram_user,
            generation_job=job,
            message=message,
            metadata=metadata,
        )
