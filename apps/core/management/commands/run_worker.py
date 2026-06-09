from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from telegram import Bot

from apps.core.models import AuditLog, GenerationJob, MediaAsset
from apps.core.services.comfyui_client import ComfyUIClient
from apps.core.services.workflow_manager import WorkflowManager


class Command(BaseCommand):
    help = "Run the serial job worker for ComfyUI generation."

    def handle(self, *args, **options) -> None:
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN) if settings.TELEGRAM_BOT_TOKEN else None
        comfy_client = ComfyUIClient()
        workflow_manager = WorkflowManager()

        self.stdout.write(self.style.SUCCESS("Worker started"))
        while True:
            job = self._claim_next_job()
            if job is None:
                time.sleep(settings.POLL_INTERVAL_SECONDS)
                continue
            self._process_job(job, bot, comfy_client, workflow_manager)

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
            job.state = GenerationJob.STATE_RUNNING
            job.started_at = timezone.now()
            job.save(update_fields=["state", "started_at", "updated_at"])
            AuditLog.objects.create(
                telegram_user=job.telegram_user,
                event_type="job_transition",
                message="running",
                payload={"job_id": job.id},
            )
            return job

    def _process_job(
        self,
        job: GenerationJob,
        bot: Bot | None,
        comfy_client: ComfyUIClient,
        workflow_manager: WorkflowManager,
    ) -> None:
        try:
            input_path = Path(job.input_asset.file.path)
            uploaded_name = comfy_client.upload_input_image(input_path)
            workflow_payload, seed = workflow_manager.build_workflow(
                input_image=uploaded_name,
                prompt=job.prompt_text or settings.DEFAULT_PROMPT,
                seed=job.seed or random.randint(1, 2**31 - 1),
            )
            job.seed = seed
            prompt_id = comfy_client.submit_workflow(workflow_payload)
            job.external_prompt_id = prompt_id
            job.save(update_fields=["seed", "external_prompt_id", "updated_at"])

            history_payload = comfy_client.wait_for_completion(prompt_id)
            outputs = comfy_client.extract_outputs(history_payload)
            if not outputs:
                raise ValueError("ComfyUI completed but returned no outputs")

            chosen_output = self._pick_output(outputs)
            output_path = self._download_output(job, comfy_client, chosen_output)
            output_asset = self._create_output_asset(job, output_path, chosen_output)

            job.output_asset = output_asset
            job.result_payload = history_payload
            job.state = GenerationJob.STATE_COMPLETED
            job.completed_at = timezone.now()
            job.error_message = ""
            job.save(
                update_fields=[
                    "output_asset",
                    "result_payload",
                    "state",
                    "completed_at",
                    "error_message",
                    "updated_at",
                ]
            )
            AuditLog.objects.create(
                telegram_user=job.telegram_user,
                event_type="job_transition",
                message="completed",
                payload={"job_id": job.id, "asset_id": output_asset.id},
            )
            if bot is not None:
                self._send_result(bot, job, output_asset)
        except Exception as exc:
            job.state = GenerationJob.STATE_FAILED
            job.error_message = str(exc)
            job.completed_at = timezone.now()
            job.save(update_fields=["state", "error_message", "completed_at", "updated_at"])
            AuditLog.objects.create(
                telegram_user=job.telegram_user,
                event_type="job_transition",
                message="failed",
                payload={"job_id": job.id, "error": str(exc)},
            )
            if bot is not None:
                asyncio.run(
                    bot.send_message(
                        chat_id=job.telegram_user.telegram_id,
                        text=f"Job #{job.id} failed: {exc}",
                    )
                )

    def _pick_output(self, outputs: list[dict]) -> dict:
        for output in outputs:
            filename = str(output.get("filename", "")).lower()
            if filename.endswith((".mp4", ".webm", ".mov")):
                return output
        return outputs[0]

    def _download_output(
        self,
        job: GenerationJob,
        comfy_client: ComfyUIClient,
        output: dict,
    ) -> Path:
        filename = output.get("filename", f"job_{job.id}_output.bin")
        target_path = Path(settings.MEDIA_ROOT) / "generated" / filename
        return comfy_client.download_output(output, target_path)

    def _create_output_asset(
        self,
        job: GenerationJob,
        output_path: Path,
        output_metadata: dict,
    ) -> MediaAsset:
        kind = MediaAsset.KIND_VIDEO
        if output_path.suffix.lower() not in {".mp4", ".webm", ".mov"}:
            kind = MediaAsset.KIND_OTHER
        with output_path.open("rb") as handle:
            return MediaAsset.objects.create(
                telegram_user=job.telegram_user,
                file=File(handle, name=f"generated/{output_path.name}"),
                kind=kind,
                source=MediaAsset.SOURCE_COMFYUI,
                original_name=output_path.name,
                prompt_text=job.prompt_text,
                metadata=output_metadata,
            )

    def _send_result(self, bot: Bot, job: GenerationJob, output_asset: MediaAsset) -> None:
        with output_asset.file.open("rb") as handle:
            if output_asset.kind == MediaAsset.KIND_VIDEO:
                asyncio.run(
                    bot.send_video(
                        chat_id=job.telegram_user.telegram_id,
                        video=handle,
                        caption=f"Job #{job.id} completed.",
                    )
                )
            else:
                asyncio.run(
                    bot.send_document(
                        chat_id=job.telegram_user.telegram_id,
                        document=handle,
                        caption=f"Job #{job.id} completed.",
                    )
                )
