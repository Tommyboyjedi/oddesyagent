from __future__ import annotations

import asyncio
import random
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone
from telegram import Bot
from telegram.request import HTTPXRequest

from apps.core.models import AuditLog, GenerationJob, MediaAsset
from apps.core.services.comfyui_client import ComfyUIClient, ComfyUIClientError
from apps.core.services.job_scheduler import JobSchedulerService
from apps.core.services.workflow_manager import WorkflowManager


class Command(BaseCommand):
    help = "Run the serial job worker for ComfyUI generation."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--once", action="store_true", help="Process at most one job then exit.")
        parser.add_argument("--sleep-seconds", type=int, default=3, help="Sleep interval when no work exists.")
        parser.add_argument("--poll-seconds", type=int, default=5, help="ComfyUI polling interval.")
        parser.add_argument("--timeout-seconds", type=int, default=1800, help="Max wait for one ComfyUI job.")

    def handle(self, *args, **options) -> None:
        bot_token = settings.TELEGRAM_BOT_TOKEN or None
        comfy_client = ComfyUIClient()
        workflow_manager = WorkflowManager()
        scheduler = JobSchedulerService()

        processed_jobs = 0
        self.stdout.write(self.style.SUCCESS("Worker started"))
        while True:
            self._recover_stale_running_jobs(comfy_client)
            job = self._claim_next_job(scheduler=scheduler)
            if job is None:
                if options["once"]:
                    self.stdout.write("No queued jobs found.")
                    return
                time.sleep(options["sleep_seconds"])
                continue

            self._process_job(
                job=job,
                bot=bot_token,
                comfy_client=comfy_client,
                workflow_manager=workflow_manager,
                poll_seconds=options["poll_seconds"],
                timeout_seconds=options["timeout_seconds"],
            )
            processed_jobs += 1
            if options["once"] and processed_jobs >= 1:
                return

    def _build_bot_request(self) -> HTTPXRequest:
        return HTTPXRequest(
            connection_pool_size=50,
            connect_timeout=10.0,
            read_timeout=120.0,
            write_timeout=120.0,
            pool_timeout=60.0,
        )

    def _claim_next_job(self, scheduler: JobSchedulerService | None = None) -> GenerationJob | None:
        return (scheduler or JobSchedulerService()).claim_next_generation_job()

    def _process_job(
        self,
        job: GenerationJob,
        bot,
        comfy_client: ComfyUIClient,
        workflow_manager: WorkflowManager,
        poll_seconds: int,
        timeout_seconds: int,
    ) -> None:
        try:
            job.refresh_from_db()
            if job.state == GenerationJob.STATE_CANCELLATION_REQUESTED:
                job.mark_cancelled()
                self._log_event(job, "job_transition", "cancelled", {"job_id": job.id})
                return

            if not job.seed:
                job.seed = random.randint(1, 2**31 - 1)

            uploaded_name: str | None = None
            image_override_ids = job.metadata.get("parsed_instruction", {}).get("image_overrides") or {}
            if workflow_manager.workflow_requires_input_media(job.workflow_name):
                if job.input_media is None:
                    if not isinstance(image_override_ids, dict) or not image_override_ids:
                        raise ValueError("Workflow requires an input image but the job has no input media.")
                else:
                    input_path = Path(job.input_media.file.path)
                    uploaded_name = comfy_client.upload_input_image(input_path)

            uploaded_image_overrides: dict[str, str] = {}
            if isinstance(image_override_ids, dict):
                for field_key, media_asset_id in image_override_ids.items():
                    try:
                        override_asset = MediaAsset.objects.get(
                            id=int(media_asset_id),
                            telegram_user=job.telegram_user,
                            asset_type__in=[MediaAsset.TYPE_INCOMING_IMAGE, MediaAsset.TYPE_GENERATED_IMAGE],
                        )
                    except (MediaAsset.DoesNotExist, TypeError, ValueError):
                        continue
                    override_path = Path(override_asset.file.path)
                    uploaded_image_overrides[str(field_key)] = comfy_client.upload_input_image(override_path)

            workflow = workflow_manager.render_generation_workflow(
                job.workflow_name,
                prompt=job.prompt,
                seed=job.seed,
                input_image=uploaded_name,
                negative_prompt=job.metadata.get("parsed_instruction", {}).get("negative_prompt"),
                length_frames=job.metadata.get("parsed_instruction", {}).get("length_frames"),
                lora_overrides=job.metadata.get("parsed_instruction", {}).get("lora_overrides"),
                text_overrides=job.metadata.get("parsed_instruction", {}).get("text_overrides"),
                image_overrides=uploaded_image_overrides,
            )
            prompt_id = comfy_client.submit_workflow(workflow)
            job.comfyui_prompt_id = prompt_id
            job.metadata["workflow_submitted"] = True
            job.save(update_fields=["seed", "comfyui_prompt_id", "metadata", "updated_at"])

            job.refresh_from_db()
            if job.state == GenerationJob.STATE_CANCELLATION_REQUESTED:
                self._log_event(
                    job,
                    "job_transition",
                    "cancellation_requested",
                    {"job_id": job.id, "prompt_id": prompt_id},
                )

            history = comfy_client.wait_for_completion(
                prompt_id=prompt_id,
                poll_seconds=poll_seconds,
                timeout_seconds=timeout_seconds,
            )
            execution_error = comfy_client.extract_execution_error_from_prompt_history(history)
            if execution_error:
                raise ComfyUIClientError(f"ComfyUI execution error: {execution_error}")
            outputs = comfy_client.extract_outputs_from_prompt_history(history)
            if not outputs:
                raise ComfyUIClientError("ComfyUI completed but returned no outputs")

            output_info = self._pick_output(outputs)
            output_asset = self._create_output_asset(job, comfy_client, output_info)
            extra_output_assets = self._create_additional_output_assets(job, comfy_client, outputs, output_asset)
            job.metadata["comfyui_history"] = history
            job.metadata["output_info"] = output_info
            job.metadata["output_summary"] = self._build_output_summary(output_asset, output_info)
            if extra_output_assets:
                job.metadata["additional_output_media_ids"] = [asset.id for asset in extra_output_assets]
            job.save(update_fields=["metadata", "updated_at"])

            job.refresh_from_db()
            if job.state == GenerationJob.STATE_CANCELLATION_REQUESTED:
                job.output_media = output_asset
                job.metadata["cancellation_result"] = {
                    "output_media_id": output_asset.id,
                    "suppressed_delivery": True,
                }
                job.save(update_fields=["output_media", "metadata", "updated_at"])
                job.mark_cancelled()
                self._log_event(
                    job,
                    "output_recorded",
                    "generated_output_saved_after_cancellation",
                    {"job_id": job.id, "output_media_id": output_asset.id},
                )
                self._log_event(
                    job,
                    "job_transition",
                    "cancelled",
                    {"job_id": job.id, "output_media_id": output_asset.id, "reason": "completed_after_cancellation_request"},
                )
                return

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
                self._notify_result(bot, job, output_asset, extra_output_assets)
        except Exception as exc:
            failure_metadata = self._classify_failure(exc)
            job.metadata["failure"] = failure_metadata
            job.save(update_fields=["metadata", "updated_at"])
            job.mark_failed(str(exc))
            self._log_event(
                job,
                "job_transition",
                "failed",
                {"job_id": job.id, "error": str(exc), **failure_metadata},
            )
            if bot is not None:
                self._notify_failure(bot, job, exc)

    def _recover_stale_running_jobs(self, comfy_client: ComfyUIClient, stale_after_seconds: int = 300) -> None:
        cutoff = timezone.now() - timedelta(seconds=stale_after_seconds)
        stale_jobs = list(
            GenerationJob.objects.filter(
                state__in=[GenerationJob.STATE_RUNNING, GenerationJob.STATE_CANCELLATION_REQUESTED],
                comfyui_prompt_id__isnull=False,
                output_media__isnull=True,
                updated_at__lt=cutoff,
            ).order_by("updated_at", "id")[:10]
        )
        for job in stale_jobs:
            self._recover_stale_job(job, comfy_client)

    def _recover_stale_job(self, job: GenerationJob, comfy_client: ComfyUIClient) -> None:
        prompt_id = (job.comfyui_prompt_id or "").strip()
        if not prompt_id:
            return
        try:
            history = comfy_client.get_history(prompt_id)
            prompt_history = history.get(prompt_id, {})
            if not prompt_history:
                return
            execution_error = comfy_client.extract_execution_error_from_prompt_history(prompt_history)
            if execution_error:
                self._mark_stale_job_failed(job, f"Recovered ComfyUI execution error: {execution_error}")
                return
            outputs = comfy_client.extract_outputs_from_prompt_history(prompt_history)
            status = prompt_history.get("status", {})
            completed = bool(status.get("completed")) or bool(outputs)
            if not completed:
                return
            output_info = self._pick_output(outputs)
            output_asset = self._create_output_asset(job, comfy_client, output_info)
            extra_output_assets = self._create_additional_output_assets(job, comfy_client, outputs, output_asset)
            job.metadata["comfyui_history"] = prompt_history
            job.metadata["output_info"] = output_info
            job.metadata["output_summary"] = self._build_output_summary(output_asset, output_info)
            job.metadata["recovery"] = {
                "status": "completed_from_history",
                "prompt_id": prompt_id,
            }
            if extra_output_assets:
                job.metadata["additional_output_media_ids"] = [asset.id for asset in extra_output_assets]
            if job.state == GenerationJob.STATE_CANCELLATION_REQUESTED:
                job.output_media = output_asset
                job.metadata["cancellation_result"] = {
                    "output_media_id": output_asset.id,
                    "suppressed_delivery": True,
                }
                job.save(update_fields=["output_media", "metadata", "updated_at"])
                job.mark_cancelled()
                self._log_event(
                    job,
                    "job_transition",
                    "cancelled",
                    {"job_id": job.id, "output_media_id": output_asset.id, "reason": "recovered_after_restart"},
                )
                return
            job.mark_completed(output_asset)
            job.metadata["recovery"]["output_media_id"] = output_asset.id
            job.save(update_fields=["metadata", "updated_at"])
            self._log_event(
                job,
                "job_transition",
                "completed",
                {"job_id": job.id, "output_media_id": output_asset.id, "recovered": True},
            )
            self._log_event(
                job,
                "output_recorded",
                "generated_output_saved_from_history",
                {"job_id": job.id, "output_media_id": output_asset.id},
            )
        except Exception as exc:
            self._log_event(
                job,
                "job_recovery_failed",
                "stale_job_recovery_failed",
                {"job_id": job.id, "prompt_id": prompt_id, "error": str(exc)},
            )

    def _mark_stale_job_failed(self, job: GenerationJob, error_message: str) -> None:
        failure_metadata = self._classify_failure(ComfyUIClientError(error_message))
        job.metadata["failure"] = failure_metadata
        job.metadata["recovery"] = {
            "status": "failed_from_history",
            "error": error_message,
            "prompt_id": job.comfyui_prompt_id,
        }
        job.save(update_fields=["metadata", "updated_at"])
        job.mark_failed(error_message)
        self._log_event(
            job,
            "job_transition",
            "failed",
            {"job_id": job.id, "error": error_message, "recovered": True, **failure_metadata},
        )

    def _pick_output(self, outputs: list[dict]) -> dict:
        for output in outputs:
            filename = str(output.get("filename", "")).lower()
            if filename.endswith((".mp4", ".webm", ".mov")):
                return output
        for output in outputs:
            if output.get("type") == "output":
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
            metadata={
                **output_info,
                "file_size_bytes": len(file_bytes),
                "output_type": asset_type,
                "duration_seconds": self._extract_duration_seconds(output_info),
                "comfyui_filename": filename,
                "comfyui_subfolder": output_info.get("subfolder", ""),
                "comfyui_output_type": output_info.get("type", "output"),
            },
        )
        output_asset.file.save(filename, ContentFile(file_bytes), save=False)
        output_asset.save()
        return output_asset

    def _create_additional_output_assets(
        self,
        job: GenerationJob,
        comfy_client: ComfyUIClient,
        outputs: list[dict],
        primary_output_asset: MediaAsset,
    ) -> list[MediaAsset]:
        additional_assets: list[MediaAsset] = []
        for output in outputs:
            if not self._is_image_output(output):
                continue
            if self._matches_asset_output(output, primary_output_asset):
                continue
            additional_assets.append(self._create_output_asset(job, comfy_client, output))
        return additional_assets

    def _is_image_output(self, output: dict[str, Any]) -> bool:
        filename = str(output.get("filename", "")).lower()
        return filename.endswith((".png", ".jpg", ".jpeg", ".webp"))

    def _matches_asset_output(self, output: dict[str, Any], asset: MediaAsset) -> bool:
        return (
            output.get("filename") == asset.metadata.get("comfyui_filename")
            and output.get("subfolder", "") == asset.metadata.get("comfyui_subfolder", "")
            and output.get("type", "output") == asset.metadata.get("comfyui_output_type", "output")
        )

    def _send_result(self, bot: Bot, job: GenerationJob, output_asset: MediaAsset) -> None:
        caption = self._build_result_caption(job, output_asset)
        with output_asset.file.open("rb") as handle:
            if output_asset.asset_type == MediaAsset.TYPE_GENERATED_VIDEO:
                asyncio.run(
                    bot.send_video(
                        chat_id=job.telegram_user.telegram_user_id,
                        video=handle,
                        caption=caption,
                    )
                )
            else:
                asyncio.run(
                    bot.send_document(
                        chat_id=job.telegram_user.telegram_user_id,
                        document=handle,
                        caption=caption,
                    )
                )

    def _notify_result(self, bot, job: GenerationJob, output_asset: MediaAsset, extra_output_assets: list[MediaAsset]) -> None:
        try:
            if isinstance(bot, str):
                asyncio.run(self._send_result_with_fresh_bot(bot, job, output_asset, extra_output_assets))
            else:
                self._send_result(bot, job, output_asset)
        except Exception as exc:
            job.metadata["delivery"] = {
                "status": "failed",
                "stage": "result",
                "error": str(exc),
                "output_media_id": output_asset.id,
            }
            job.save(update_fields=["metadata", "updated_at"])
            self._log_event(
                job,
                "delivery_failed",
                "result_delivery_failed",
                {"job_id": job.id, "output_media_id": output_asset.id, "error": str(exc)},
            )

    def _notify_failure(self, bot, job: GenerationJob, exc: Exception) -> None:
        try:
            if isinstance(bot, str):
                asyncio.run(self._send_failure_with_fresh_bot(bot, job, exc))
            else:
                asyncio.run(
                    bot.send_message(
                        chat_id=job.telegram_user.telegram_user_id,
                        text=f"Job #{job.id} failed: {exc}",
                    )
                )
        except Exception as notification_exc:
            job.metadata["delivery"] = {
                "status": "failed",
                "stage": "failure_notice",
                "error": str(notification_exc),
            }
            job.save(update_fields=["metadata", "updated_at"])
            self._log_event(
                job,
                "delivery_failed",
                "failure_notice_delivery_failed",
                {"job_id": job.id, "error": str(notification_exc)},
            )

    async def _send_result_with_fresh_bot(
        self,
        bot_token: str,
        job: GenerationJob,
        output_asset: MediaAsset,
        extra_output_assets: list[MediaAsset],
    ) -> None:
        async with Bot(token=bot_token, request=self._build_bot_request()) as bot:
            await self._send_asset_via_bot(
                bot,
                job,
                output_asset,
                caption=self._build_result_caption(job, output_asset),
            )
            if (
                job.telegram_user.image_output_mode == job.telegram_user.IMAGE_OUTPUT_MODE_ALL
                and output_asset.asset_type == MediaAsset.TYPE_GENERATED_IMAGE
            ):
                for index, asset in enumerate(extra_output_assets, start=2):
                    await self._send_asset_via_bot(
                        bot,
                        job,
                        asset,
                        caption=f"Job #{job.id} additional image #{index}.",
                    )

    async def _send_failure_with_fresh_bot(self, bot_token: str, job: GenerationJob, exc: Exception) -> None:
        async with Bot(token=bot_token, request=self._build_bot_request()) as bot:
            await bot.send_message(
                chat_id=job.telegram_user.telegram_user_id,
                text=f"Job #{job.id} failed: {exc}",
            )

    async def _send_asset_via_bot(self, bot: Bot, job: GenerationJob, asset: MediaAsset, caption: str) -> None:
        with asset.file.open("rb") as handle:
            if asset.asset_type == MediaAsset.TYPE_GENERATED_VIDEO:
                await bot.send_video(
                    chat_id=job.telegram_user.telegram_user_id,
                    video=handle,
                    caption=caption,
                )
            else:
                await bot.send_document(
                    chat_id=job.telegram_user.telegram_user_id,
                    document=handle,
                    caption=caption,
                )

    def _build_result_caption(self, job: GenerationJob, asset: MediaAsset) -> str:
        return f"Job #{job.id} completed. Media #{asset.id}."

    def _log_event(self, job: GenerationJob, event_type: str, message: str, metadata: dict) -> None:
        AuditLog.objects.create(
            event_type=event_type,
            telegram_user=job.telegram_user,
            generation_job=job,
            message=message,
            metadata=metadata,
        )

    def _classify_failure(self, exc: Exception) -> dict[str, Any]:
        message = str(exc)
        if isinstance(exc, FileNotFoundError):
            return {"failure_type": "workflow_missing", "retry_safe": True}
        if isinstance(exc, ValueError) and "unresolved placeholders" in message.lower():
            return {"failure_type": "placeholder_missing", "retry_safe": True}
        if isinstance(exc, ComfyUIClientError):
            lowered = message.lower()
            if "timed out waiting" in lowered:
                return {"failure_type": "timeout", "retry_safe": True}
            if "execution error" in lowered:
                return {"failure_type": "comfyui_execution_error", "retry_safe": True}
            if "returned no outputs" in lowered or "download output file" in lowered:
                return {"failure_type": "output_missing", "retry_safe": True}
            return {"failure_type": "comfyui_unavailable", "retry_safe": True}
        return {"failure_type": "unknown", "retry_safe": False}

    def _extract_duration_seconds(self, output_info: dict[str, Any]) -> float | None:
        for key in ("duration_seconds", "duration", "seconds"):
            value = output_info.get(key)
            if value in [None, ""]:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _build_output_summary(self, output_asset: MediaAsset, output_info: dict[str, Any]) -> dict[str, Any]:
        return {
            "media_asset_id": output_asset.id,
            "asset_type": output_asset.asset_type,
            "file_size_bytes": output_asset.metadata.get("file_size_bytes"),
            "duration_seconds": output_asset.metadata.get("duration_seconds"),
            "comfyui_filename": output_info.get("filename", output_asset.original_file_name),
            "comfyui_subfolder": output_info.get("subfolder", ""),
            "comfyui_output_type": output_info.get("type", "output"),
        }
