from __future__ import annotations

from pathlib import Path

from apps.core.models import GenerationJob, MediaAsset, TelegramUser
from apps.core.services.job_service import JobService
from apps.core.services.workflow_manager import WorkflowManager


class OddesyAgentService:
    def __init__(
        self,
        job_service: JobService | None = None,
        workflow_manager: WorkflowManager | None = None,
    ) -> None:
        self.job_service = job_service or JobService()
        self.workflow_manager = workflow_manager or WorkflowManager()

    def list_workflows(self) -> list[str]:
        return self.workflow_manager.list_workflows()

    def workflow_exists(self, workflow_name: str) -> bool:
        normalized_name = workflow_name[:-5] if workflow_name.endswith(".json") else workflow_name
        return normalized_name in self.workflow_manager.list_workflows()

    def get_latest_input_media(self, telegram_user: TelegramUser) -> MediaAsset | None:
        return (
            telegram_user.media_assets.filter(asset_type=MediaAsset.TYPE_INCOMING_IMAGE)
            .order_by("-created_at", "-id")
            .first()
        )

    def get_latest_generated_media(self, telegram_user: TelegramUser) -> MediaAsset | None:
        queryset = telegram_user.media_assets.filter(
            asset_type__in=[MediaAsset.TYPE_GENERATED_VIDEO, MediaAsset.TYPE_GENERATED_IMAGE]
        ).order_by("-created_at", "-id")
        for asset in queryset:
            if self._is_media_asset_available(asset):
                return asset
        return None

    def get_available_media(self, telegram_user: TelegramUser, asset_types: list[str] | None = None, limit: int = 20) -> list[MediaAsset]:
        queryset = telegram_user.media_assets.order_by("-created_at", "-id")
        if asset_types:
            queryset = queryset.filter(asset_type__in=asset_types)
        assets: list[MediaAsset] = []
        for asset in queryset:
            if self._is_media_asset_available(asset):
                assets.append(asset)
            if len(assets) >= limit:
                break
        return assets

    def _is_media_asset_available(self, asset: MediaAsset) -> bool:
        cleanup_metadata = asset.metadata.get("cleanup", {})
        if cleanup_metadata.get("removed_from_library", False):
            return False
        try:
            return Path(asset.file.path).exists()
        except (ValueError, OSError):
            return False

    def create_job_from_existing_media(
        self,
        telegram_user: TelegramUser,
        media_asset: MediaAsset,
        workflow_name: str,
        prompt: str,
        seed: int = 0,
        priority: int | None = None,
        requested_executor: str = GenerationJob.EXECUTOR_LOCAL_GPU,
        metadata: dict | None = None,
    ) -> GenerationJob:
        if media_asset.telegram_user_id != telegram_user.id:
            raise ValueError("Media asset does not belong to the requesting Telegram user.")
        if not self.workflow_exists(workflow_name):
            raise ValueError(f"Workflow not found: {workflow_name}")
        return self.job_service.create_generation_job(
            telegram_user=telegram_user,
            input_media=media_asset,
            workflow_name=workflow_name,
            prompt=prompt,
            seed=seed,
            priority=priority,
            requested_executor=requested_executor,
            metadata=metadata,
        )

    def get_job(self, telegram_user: TelegramUser, job_id: int) -> GenerationJob | None:
        return telegram_user.generation_jobs.filter(id=job_id).first()

    def get_job_status_payload(self, telegram_user: TelegramUser, job_id: int) -> dict | None:
        job = self.get_job(telegram_user, job_id)
        if job is None:
            return None
        return self._serialize_job(job)

    def list_media_payloads(
        self,
        telegram_user: TelegramUser,
        asset_types: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        return [self._serialize_media(asset) for asset in self.get_available_media(telegram_user, asset_types, limit)]

    def get_generated_output_payload(self, telegram_user: TelegramUser, job_id: int) -> dict | None:
        job = self.get_job(telegram_user, job_id)
        if job is None or job.output_media is None:
            return None
        if not self._is_media_asset_available(job.output_media):
            return None
        return self._serialize_media(job.output_media)

    def _serialize_job(self, job: GenerationJob) -> dict:
        return {
            "id": job.id,
            "state": job.state,
            "workflow_name": job.workflow_name,
            "prompt": job.prompt,
            "seed": job.seed,
            "priority": job.priority,
            "requested_executor": job.requested_executor,
            "input_media_id": job.input_media_id,
            "output_media_id": job.output_media_id,
            "comfyui_prompt_id": job.comfyui_prompt_id,
            "error_message": job.error_message,
            "created_at": job.created_at.isoformat(),
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "metadata": job.metadata,
        }

    def _serialize_media(self, asset: MediaAsset) -> dict:
        return {
            "id": asset.id,
            "asset_type": asset.asset_type,
            "original_file_name": asset.original_file_name,
            "telegram_file_id": asset.telegram_file_id,
            "file_name": asset.file.name,
            "created_at": asset.created_at.isoformat(),
            "metadata": asset.metadata,
        }
