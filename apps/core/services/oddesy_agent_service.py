from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile

from apps.core.models import GenerationJob, MediaAsset, TelegramUser
from apps.core.services.job_service import JobService
from apps.core.services.telegram_imageswap_service import TelegramImageSwapService
from apps.core.services.video_combination import VideoCombinationService
from apps.core.services.video_last_frame_enhancement import VideoLastFrameEnhancementService
from apps.core.services.workflow_manager import WorkflowManager


class OddesyAgentService:
    LAST_FRAME_UPSCALE_WORKFLOW_NAME = "Oddesy Last Frame Upscale"

    def __init__(
        self,
        job_service: JobService | None = None,
        workflow_manager: WorkflowManager | None = None,
        telegram_imageswap_service: TelegramImageSwapService | None = None,
        video_last_frame_enhancement_service: VideoLastFrameEnhancementService | None = None,
        video_combination_service: VideoCombinationService | None = None,
    ) -> None:
        self.job_service = job_service or JobService()
        self.workflow_manager = workflow_manager or WorkflowManager()
        self.telegram_imageswap_service = telegram_imageswap_service or TelegramImageSwapService()
        self.video_last_frame_enhancement_service = (
            video_last_frame_enhancement_service or VideoLastFrameEnhancementService()
        )
        self.video_combination_service = video_combination_service or VideoCombinationService()

    def list_workflows(self) -> list[str]:
        return self.workflow_manager.list_workflows()

    def get_active_text_workflow(self, telegram_user: TelegramUser) -> str:
        configured = telegram_user.active_text_workflow or ""
        if configured:
            resolved = self.resolve_workflow_name(configured)
            if resolved is not None:
                return resolved
        resolved_default = self.resolve_workflow_name("jugg_latent_cyberpony")
        if resolved_default is not None:
            return resolved_default
        return self.resolve_workflow_name(settings.TEXT_TO_IMAGE_WORKFLOW_NAME) or settings.TEXT_TO_IMAGE_WORKFLOW_NAME

    def get_active_video_workflow(self, telegram_user: TelegramUser) -> str:
        configured = telegram_user.active_video_workflow or ""
        if configured:
            resolved = self.resolve_workflow_name(configured)
            if resolved is not None:
                return resolved
        return self.resolve_workflow_name(settings.DEFAULT_WORKFLOW_NAME) or settings.DEFAULT_WORKFLOW_NAME

    def list_power_lora_slots(self, workflow_name: str) -> list[dict]:
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        return self.workflow_manager.inspect_power_lora_slots(resolved_name)

    def list_workflow_text_fields(self, workflow_name: str) -> list[dict]:
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        return self.workflow_manager.inspect_text_prompt_fields(resolved_name)

    def get_workflow_lora_overrides(self, telegram_user: TelegramUser, workflow_name: str) -> dict[str, dict]:
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        overrides = telegram_user.workflow_lora_overrides or {}
        workflow_overrides = overrides.get(resolved_name, {})
        return {str(key): value for key, value in workflow_overrides.items() if isinstance(value, dict)}

    def get_workflow_text_overrides(self, telegram_user: TelegramUser, workflow_name: str) -> dict[str, str]:
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        overrides = telegram_user.workflow_text_overrides or {}
        workflow_overrides = overrides.get(resolved_name, {})
        return {
            str(key): str(value)
            for key, value in workflow_overrides.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def get_workflow_image_overrides(self, telegram_user: TelegramUser, workflow_name: str) -> dict[str, int]:
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        overrides = telegram_user.workflow_image_overrides or {}
        workflow_overrides = overrides.get(resolved_name, {})
        normalized: dict[str, int] = {}
        for key, value in workflow_overrides.items():
            if not isinstance(key, str):
                continue
            try:
                normalized[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return normalized

    def get_effective_workflow_text_fields(self, telegram_user: TelegramUser, workflow_name: str) -> list[dict]:
        fields = self.list_workflow_text_fields(workflow_name)
        overrides = self.get_workflow_text_overrides(telegram_user, workflow_name)
        effective: list[dict] = []
        for field in fields:
            merged = dict(field)
            if field["key"] in overrides:
                merged["value"] = overrides[field["key"]]
                merged["overridden"] = True
            else:
                merged["overridden"] = False
            effective.append(merged)
        return effective

    def list_workflow_image_fields(self, workflow_name: str) -> list[dict]:
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        return self.workflow_manager.inspect_image_input_fields(resolved_name)

    def get_effective_workflow_image_fields(self, telegram_user: TelegramUser, workflow_name: str) -> list[dict]:
        fields = self.list_workflow_image_fields(workflow_name)
        overrides = self.get_workflow_image_overrides(telegram_user, workflow_name)
        effective: list[dict] = []
        for field in fields:
            merged = dict(field)
            override_id = overrides.get(field["key"])
            if override_id is None:
                merged["overridden"] = False
                merged["media_asset_id"] = None
                effective.append(merged)
                continue
            media_asset = self.get_image_media_by_id(telegram_user, override_id)
            merged["overridden"] = True
            merged["media_asset_id"] = override_id
            merged["media_asset_name"] = media_asset.original_file_name if media_asset is not None else ""
            effective.append(merged)
        return effective

    def get_effective_power_lora_slots(self, telegram_user: TelegramUser, workflow_name: str) -> list[dict]:
        slots = self.list_power_lora_slots(workflow_name)
        overrides = self.get_workflow_lora_overrides(telegram_user, workflow_name)
        effective: list[dict] = []
        for slot in slots:
            override = overrides.get(str(slot["slot"]), {})
            merged = dict(slot)
            if "on" in override:
                merged["on"] = bool(override["on"])
            if "lora" in override:
                merged["lora"] = str(override["lora"] or "")
            if "strength" in override and override["strength"] is not None:
                merged["strength"] = float(override["strength"])
            merged["overridden"] = bool(override)
            effective.append(merged)
        return effective

    def set_workflow_lora_override(
        self,
        telegram_user: TelegramUser,
        workflow_name: str,
        slot: int,
        *,
        on: bool | None = None,
        lora: str | None = None,
        strength: float | None = None,
    ) -> dict:
        if slot < 1:
            raise ValueError("LoRA slot must be 1 or greater.")
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        slots = self.list_power_lora_slots(resolved_name)
        if slot > len(slots):
            raise ValueError(f"Workflow '{resolved_name}' only has {len(slots)} Power LoRA slot(s).")
        overrides = dict(telegram_user.workflow_lora_overrides or {})
        workflow_overrides = dict(overrides.get(resolved_name, {}))
        slot_override = dict(workflow_overrides.get(str(slot), {}))
        if on is not None:
            slot_override["on"] = bool(on)
        if lora is not None:
            slot_override["lora"] = lora.strip()
        if strength is not None:
            slot_override["strength"] = float(strength)
        workflow_overrides[str(slot)] = slot_override
        overrides[resolved_name] = workflow_overrides
        telegram_user.workflow_lora_overrides = overrides
        telegram_user.save(update_fields=["workflow_lora_overrides", "updated_at"])
        return slot_override

    def clear_workflow_lora_override(self, telegram_user: TelegramUser, workflow_name: str, slot: int) -> None:
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        overrides = dict(telegram_user.workflow_lora_overrides or {})
        workflow_overrides = dict(overrides.get(resolved_name, {}))
        workflow_overrides.pop(str(slot), None)
        if workflow_overrides:
            overrides[resolved_name] = workflow_overrides
        else:
            overrides.pop(resolved_name, None)
        telegram_user.workflow_lora_overrides = overrides
        telegram_user.save(update_fields=["workflow_lora_overrides", "updated_at"])

    def set_workflow_text_override(
        self,
        telegram_user: TelegramUser,
        workflow_name: str,
        field_key: str,
        value: str,
    ) -> str:
        normalized_key = field_key.strip().lower()
        normalized_value = value.strip()
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        valid_keys = {str(field["key"]) for field in self.list_workflow_text_fields(resolved_name)}
        if normalized_key not in valid_keys:
            raise ValueError(f"Workflow '{resolved_name}' has no text field named '{field_key}'.")
        overrides = dict(telegram_user.workflow_text_overrides or {})
        workflow_overrides = dict(overrides.get(resolved_name, {}))
        workflow_overrides[normalized_key] = normalized_value
        overrides[resolved_name] = workflow_overrides
        telegram_user.workflow_text_overrides = overrides
        telegram_user.save(update_fields=["workflow_text_overrides", "updated_at"])
        return normalized_value

    def clear_workflow_text_override(self, telegram_user: TelegramUser, workflow_name: str, field_key: str) -> None:
        normalized_key = field_key.strip().lower()
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        overrides = dict(telegram_user.workflow_text_overrides or {})
        workflow_overrides = dict(overrides.get(resolved_name, {}))
        workflow_overrides.pop(normalized_key, None)
        if workflow_overrides:
            overrides[resolved_name] = workflow_overrides
        else:
            overrides.pop(resolved_name, None)
        telegram_user.workflow_text_overrides = overrides
        telegram_user.save(update_fields=["workflow_text_overrides", "updated_at"])

    def set_workflow_image_override(
        self,
        telegram_user: TelegramUser,
        workflow_name: str,
        field_key: str,
        media_asset_id: int,
    ) -> int:
        normalized_key = field_key.strip().lower()
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        valid_keys = {str(field["key"]) for field in self.list_workflow_image_fields(resolved_name)}
        if normalized_key not in valid_keys:
            raise ValueError(f"Workflow '{resolved_name}' has no image field named '{field_key}'.")
        media_asset = self.get_image_media_by_id(telegram_user, media_asset_id)
        if media_asset is None:
            raise ValueError(f"Image media #{media_asset_id} was not found.")
        overrides = dict(telegram_user.workflow_image_overrides or {})
        workflow_overrides = dict(overrides.get(resolved_name, {}))
        workflow_overrides[normalized_key] = int(media_asset.id)
        overrides[resolved_name] = workflow_overrides
        telegram_user.workflow_image_overrides = overrides
        telegram_user.save(update_fields=["workflow_image_overrides", "updated_at"])
        return int(media_asset.id)

    def clear_workflow_image_override(self, telegram_user: TelegramUser, workflow_name: str, field_key: str) -> None:
        normalized_key = field_key.strip().lower()
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        overrides = dict(telegram_user.workflow_image_overrides or {})
        workflow_overrides = dict(overrides.get(resolved_name, {}))
        workflow_overrides.pop(normalized_key, None)
        if workflow_overrides:
            overrides[resolved_name] = workflow_overrides
        else:
            overrides.pop(resolved_name, None)
        telegram_user.workflow_image_overrides = overrides
        telegram_user.save(update_fields=["workflow_image_overrides", "updated_at"])

    def get_image_output_mode(self, telegram_user: TelegramUser) -> str:
        if telegram_user.image_output_mode in {
            TelegramUser.IMAGE_OUTPUT_MODE_SAVED,
            TelegramUser.IMAGE_OUTPUT_MODE_ALL,
        }:
            return telegram_user.image_output_mode
        return TelegramUser.IMAGE_OUTPUT_MODE_SAVED

    def get_generation_batch_count(self, telegram_user: TelegramUser) -> int:
        count = int(telegram_user.generation_batch_count or TelegramUser.DEFAULT_GENERATION_BATCH_COUNT)
        return max(1, min(count, TelegramUser.MAX_GENERATION_BATCH_COUNT))

    def set_generation_batch_count(self, telegram_user: TelegramUser, count: int | str) -> int:
        try:
            normalized = int(str(count).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Generation batch count must be a whole number.") from exc
        if normalized < 1 or normalized > TelegramUser.MAX_GENERATION_BATCH_COUNT:
            raise ValueError(
                f"Generation batch count must be between 1 and {TelegramUser.MAX_GENERATION_BATCH_COUNT}."
            )
        telegram_user.generation_batch_count = normalized
        telegram_user.save(update_fields=["generation_batch_count", "updated_at"])
        return normalized

    def get_musicvideo_working_project(self, telegram_user: TelegramUser) -> str:
        return (telegram_user.musicvideo_working_project or "").strip()

    def set_musicvideo_working_project(self, telegram_user: TelegramUser, project_id: str) -> str:
        normalized = project_id.strip()
        telegram_user.musicvideo_working_project = normalized
        telegram_user.save(update_fields=["musicvideo_working_project", "updated_at"])
        return normalized

    def clear_musicvideo_working_project(self, telegram_user: TelegramUser) -> None:
        telegram_user.musicvideo_working_project = ""
        telegram_user.save(update_fields=["musicvideo_working_project", "updated_at"])

    def get_active_video_negative_prompt(self, telegram_user: TelegramUser) -> str:
        return (telegram_user.active_video_negative_prompt or "").strip()

    def set_active_video_negative_prompt(self, telegram_user: TelegramUser, prompt: str) -> str:
        normalized = prompt.strip()
        telegram_user.active_video_negative_prompt = normalized
        telegram_user.save(update_fields=["active_video_negative_prompt", "updated_at"])
        return normalized

    def get_active_video_length_frames(self, telegram_user: TelegramUser) -> int | None:
        value = telegram_user.active_video_length_frames
        if value is None:
            return None
        return int(value)

    def get_imageswap_defaults(self, telegram_user: TelegramUser) -> dict:
        return self.telegram_imageswap_service.get_defaults(
            telegram_user,
            resolve_workflow_name=self.resolve_workflow_name,
            get_image_media_by_id=lambda media_asset_id: self.get_image_media_by_id(telegram_user, media_asset_id),
        )

    def set_imageswap_defaults(
        self,
        telegram_user: TelegramUser,
        *,
        workflow_name: str | None = None,
        sam_prompt_text: str | None = None,
        swap_kind: str | None = None,
        mode: str | None = None,
        target_media_asset_id: int | None = None,
        positive_prompt: str | None = None,
        swap_text_prompt: str | None = None,
        swap_reference_media_asset_id: int | None = None,
    ) -> dict:
        return self.telegram_imageswap_service.set_defaults(
            telegram_user,
            workflow_name=workflow_name,
            sam_prompt_text=sam_prompt_text,
            swap_kind=swap_kind,
            mode=mode,
            resolve_workflow_name=self.resolve_workflow_name,
            target_media_asset_id=target_media_asset_id,
            positive_prompt=positive_prompt,
            swap_text_prompt=swap_text_prompt,
            swap_reference_media_asset_id=swap_reference_media_asset_id,
            get_image_media_by_id=lambda media_asset_id: self.get_image_media_by_id(telegram_user, media_asset_id),
        )

    def clear_imageswap_defaults(self, telegram_user: TelegramUser) -> None:
        self.telegram_imageswap_service.clear_defaults(telegram_user)

    def get_imageswap_draft(self, telegram_user: TelegramUser) -> dict:
        return self.telegram_imageswap_service.get_draft(
            telegram_user,
            get_image_media_by_id=lambda media_asset_id: self.get_image_media_by_id(telegram_user, media_asset_id),
        )

    def set_imageswap_draft(
        self,
        telegram_user: TelegramUser,
        *,
        sam_prompt_text: str | None = None,
        swap_kind: str | None = None,
        target_media_asset_id: int | None = None,
        positive_prompt: str | None = None,
        swap_text_prompt: str | None = None,
        swap_reference_media_asset_id: int | None = None,
    ) -> dict:
        return self.telegram_imageswap_service.set_draft(
            telegram_user,
            sam_prompt_text=sam_prompt_text,
            swap_kind=swap_kind,
            target_media_asset_id=target_media_asset_id,
            positive_prompt=positive_prompt,
            swap_text_prompt=swap_text_prompt,
            swap_reference_media_asset_id=swap_reference_media_asset_id,
            get_image_media_by_id=lambda media_asset_id: self.get_image_media_by_id(telegram_user, media_asset_id),
        )

    def clear_imageswap_draft(self, telegram_user: TelegramUser) -> None:
        self.telegram_imageswap_service.clear_draft(telegram_user)

    def build_imageswap_request(self, telegram_user: TelegramUser) -> dict:
        return self.telegram_imageswap_service.build_request(
            telegram_user,
            resolve_workflow_name=self.resolve_workflow_name,
            get_image_media_by_id=lambda media_asset_id: self.get_image_media_by_id(telegram_user, media_asset_id),
            list_workflow_text_fields=self.list_workflow_text_fields,
            list_workflow_image_fields=self.list_workflow_image_fields,
            workflow_requires_input_media=self.workflow_requires_input_media,
        )

    def set_active_video_length_frames(self, telegram_user: TelegramUser, length_frames: int | str | None) -> int | None:
        if length_frames in (None, ""):
            telegram_user.active_video_length_frames = None
            telegram_user.save(update_fields=["active_video_length_frames", "updated_at"])
            return None
        try:
            normalized = int(str(length_frames).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Video length must be a whole number of frames.") from exc
        if normalized < 1:
            raise ValueError("Video length must be at least 1 frame.")
        telegram_user.active_video_length_frames = normalized
        telegram_user.save(update_fields=["active_video_length_frames", "updated_at"])
        return normalized

    def set_image_output_mode(self, telegram_user: TelegramUser, mode: str) -> str:
        normalized = mode.strip().lower()
        if normalized not in {
            TelegramUser.IMAGE_OUTPUT_MODE_SAVED,
            TelegramUser.IMAGE_OUTPUT_MODE_ALL,
        }:
            raise ValueError("Image output mode must be 'saved' or 'all'.")
        telegram_user.image_output_mode = normalized
        telegram_user.save(update_fields=["image_output_mode", "updated_at"])
        return normalized

    def set_active_text_workflow(self, telegram_user: TelegramUser, workflow_name: str) -> str:
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        telegram_user.active_text_workflow = resolved_name
        telegram_user.save(update_fields=["active_text_workflow", "updated_at"])
        return resolved_name

    def set_active_video_workflow(self, telegram_user: TelegramUser, workflow_name: str) -> str:
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        telegram_user.active_video_workflow = resolved_name
        telegram_user.save(update_fields=["active_video_workflow", "updated_at"])
        return resolved_name

    def workflow_exists(self, workflow_name: str) -> bool:
        return self.resolve_workflow_name(workflow_name) is not None

    def resolve_workflow_name(self, workflow_name: str) -> str | None:
        normalized_name = workflow_name[:-5] if workflow_name.endswith(".json") else workflow_name
        lowered = normalized_name.strip().lower()
        workflows = self.workflow_manager.list_workflows()
        if normalized_name in workflows:
            return normalized_name

        exact_matches = [workflow for workflow in workflows if workflow.lower() == lowered]
        if exact_matches:
            return exact_matches[0]

        prefix_matches = [workflow for workflow in workflows if workflow.lower().startswith(lowered)]
        if len(prefix_matches) == 1:
            return prefix_matches[0]

        contains_matches = [workflow for workflow in workflows if lowered in workflow.lower()]
        if len(contains_matches) == 1:
            return contains_matches[0]

        return None

    def workflow_requires_input_media(self, workflow_name: str) -> bool:
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        return self.workflow_manager.workflow_requires_input_media(resolved_name)

    def get_latest_input_media(self, telegram_user: TelegramUser) -> MediaAsset | None:
        return (
            telegram_user.media_assets.filter(asset_type=MediaAsset.TYPE_INCOMING_IMAGE)
            .order_by("-created_at", "-id")
            .first()
        )

    def set_pending_video_media(self, telegram_user: TelegramUser, media_asset: MediaAsset | None) -> None:
        pending_id = media_asset.id if media_asset is not None else None
        if telegram_user.pending_video_media_asset_id == pending_id:
            return
        telegram_user.pending_video_media_asset_id = pending_id
        telegram_user.save(update_fields=["pending_video_media_asset_id", "updated_at"])

    def clear_pending_video_media(self, telegram_user: TelegramUser) -> None:
        self.set_pending_video_media(telegram_user, None)

    def get_pending_video_media(self, telegram_user: TelegramUser) -> MediaAsset | None:
        pending_id = telegram_user.pending_video_media_asset_id
        if pending_id is None:
            return None
        try:
            asset = telegram_user.media_assets.get(id=pending_id, asset_type=MediaAsset.TYPE_INCOMING_IMAGE)
        except MediaAsset.DoesNotExist:
            self.clear_pending_video_media(telegram_user)
            return None
        if not self._is_media_asset_available(asset):
            self.clear_pending_video_media(telegram_user)
            return None
        return asset

    def get_latest_generated_media(self, telegram_user: TelegramUser) -> MediaAsset | None:
        queryset = telegram_user.media_assets.filter(
            asset_type__in=[MediaAsset.TYPE_GENERATED_VIDEO, MediaAsset.TYPE_GENERATED_IMAGE]
        ).order_by("-created_at", "-id")
        for asset in queryset:
            if self._is_media_asset_available(asset):
                return asset
        return None

    def get_latest_generated_image(self, telegram_user: TelegramUser) -> MediaAsset | None:
        queryset = telegram_user.media_assets.filter(
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE
        ).order_by("-created_at", "-id")
        for asset in queryset:
            if self._is_media_asset_available(asset):
                return asset
        return None

    def get_image_media_by_id(self, telegram_user: TelegramUser, media_asset_id: int) -> MediaAsset | None:
        try:
            normalized_id = int(media_asset_id)
        except (TypeError, ValueError):
            return None
        queryset = telegram_user.media_assets.filter(
            id=normalized_id,
            asset_type__in=[MediaAsset.TYPE_INCOMING_IMAGE, MediaAsset.TYPE_GENERATED_IMAGE],
        )
        asset = queryset.first()
        if asset is None or not self._is_media_asset_available(asset):
            return None
        return asset

    def get_latest_generated_video(self, telegram_user: TelegramUser) -> MediaAsset | None:
        queryset = telegram_user.media_assets.filter(
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO
        ).order_by("-created_at", "-id")
        for asset in queryset:
            if self._is_media_asset_available(asset):
                return asset
        return None

    def get_latest_video(self, telegram_user: TelegramUser) -> MediaAsset | None:
        queryset = telegram_user.media_assets.filter(
            asset_type__in=[MediaAsset.TYPE_INCOMING_VIDEO, MediaAsset.TYPE_GENERATED_VIDEO]
        ).order_by("-created_at", "-id")
        for asset in queryset:
            if self._is_media_asset_available(asset):
                return asset
        return None

    def get_video_media_by_id(self, telegram_user: TelegramUser, media_asset_id: int) -> MediaAsset | None:
        asset = telegram_user.media_assets.filter(
            id=media_asset_id,
            asset_type__in=[MediaAsset.TYPE_INCOMING_VIDEO, MediaAsset.TYPE_GENERATED_VIDEO],
        ).first()
        if asset is None or not self._is_media_asset_available(asset):
            return None
        return asset

    def list_recent_videos(self, telegram_user: TelegramUser, limit: int = 10) -> list[MediaAsset]:
        return self.get_available_media(
            telegram_user,
            asset_types=[MediaAsset.TYPE_INCOMING_VIDEO, MediaAsset.TYPE_GENERATED_VIDEO],
            limit=limit,
        )

    def enhance_latest_video_last_frame(
        self,
        telegram_user: TelegramUser,
        *,
        upscale_factor: float = 2.0,
        sharpen_amount: float = 0.4,
    ) -> MediaAsset | None:
        video_asset = self.get_latest_video(telegram_user)
        if video_asset is None:
            return None
        return self.enhance_video_last_frame(
            telegram_user,
            video_asset,
            upscale_factor=upscale_factor,
            sharpen_amount=sharpen_amount,
        )

    def enhance_video_last_frame_by_id(
        self,
        telegram_user: TelegramUser,
        media_asset_id: int,
        *,
        upscale_factor: float = 2.0,
        sharpen_amount: float = 0.4,
    ) -> MediaAsset | None:
        video_asset = self.get_video_media_by_id(telegram_user, media_asset_id)
        if video_asset is None:
            return None
        return self.enhance_video_last_frame(
            telegram_user,
            video_asset,
            upscale_factor=upscale_factor,
            sharpen_amount=sharpen_amount,
        )

    def enhance_video_last_frame(
        self,
        telegram_user: TelegramUser,
        video_asset: MediaAsset,
        *,
        upscale_factor: float = 2.0,
        sharpen_amount: float = 0.4,
    ) -> MediaAsset:
        if video_asset.telegram_user_id != telegram_user.id:
            raise ValueError("Media asset does not belong to the requesting Telegram user.")
        if video_asset.asset_type not in {MediaAsset.TYPE_INCOMING_VIDEO, MediaAsset.TYPE_GENERATED_VIDEO}:
            raise ValueError("Last-frame enhancement requires a saved video asset.")
        if not self._is_media_asset_available(video_asset):
            raise ValueError("Video asset is not available on disk.")

        source_path = Path(video_asset.file.path)
        temp_output_path = source_path.with_name(f"{source_path.stem}_last_frame_enhanced.png")
        roots = [str(Path(settings.MEDIA_ROOT).resolve())]
        result = self.video_last_frame_enhancement_service.enhance_last_frame(
            video_path=str(source_path),
            output_path=str(temp_output_path),
            upscale_factor=upscale_factor,
            sharpen_amount=sharpen_amount,
            roots=roots,
        )

        enhanced_path = Path(result["output_path"])
        image_bytes = enhanced_path.read_bytes()
        enhanced_asset = MediaAsset(
            telegram_user=telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name=enhanced_path.name,
            metadata={
                "derived_from_media_asset_id": video_asset.id,
                "source_media_asset_type": video_asset.asset_type,
                "enhancement": {
                    "kind": "last_frame_oversample",
                    "upscale_factor": upscale_factor,
                    "sharpen_amount": sharpen_amount,
                    "frame_count": result.get("frame_count"),
                    "source_video_path": str(source_path),
                },
                "file_size_bytes": len(image_bytes),
            },
        )
        enhanced_asset.file.save(enhanced_path.name, ContentFile(image_bytes), save=False)
        enhanced_asset.save()
        try:
            enhanced_path.unlink(missing_ok=True)
        except OSError:
            pass
        return enhanced_asset

    def queue_last_frame_upscale_job(self, telegram_user: TelegramUser, media_asset_id: int | None = None) -> GenerationJob | None:
        video_asset = (
            self.get_latest_generated_video(telegram_user)
            if media_asset_id is None
            else self.get_video_media_by_id(telegram_user, media_asset_id)
        )
        if video_asset is None:
            return None
        if video_asset.telegram_user_id != telegram_user.id:
            raise ValueError("Media asset does not belong to the requesting Telegram user.")
        if video_asset.asset_type != MediaAsset.TYPE_GENERATED_VIDEO:
            raise ValueError("Last-frame upscale requires a generated video asset.")
        if not self._is_media_asset_available(video_asset):
            raise ValueError("Video asset is not available on disk.")

        source_path = Path(video_asset.file.path)
        extracted_frame_path = source_path.with_name(f"{source_path.stem}_last_frame_source.png")
        roots = [str(Path(settings.MEDIA_ROOT).resolve())]
        result = self.video_last_frame_enhancement_service.extract_last_frame(
            video_path=str(source_path),
            output_path=str(extracted_frame_path),
            roots=roots,
        )

        frame_path = Path(result["output_path"])
        image_bytes = frame_path.read_bytes()
        input_asset = MediaAsset(
            telegram_user=telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name=frame_path.name,
            metadata={
                "derived_from_media_asset_id": video_asset.id,
                "source_media_asset_type": video_asset.asset_type,
                "extraction": {
                    "kind": "last_frame",
                    "frame_count": result.get("frame_count"),
                    "source_video_path": str(source_path),
                },
                "file_size_bytes": len(image_bytes),
            },
        )
        input_asset.file.save(frame_path.name, ContentFile(image_bytes), save=False)
        input_asset.save()
        try:
            frame_path.unlink(missing_ok=True)
        except OSError:
            pass

        workflow_name = self.resolve_workflow_name(self.LAST_FRAME_UPSCALE_WORKFLOW_NAME)
        if workflow_name is None:
            raise ValueError(f"Workflow not found: {self.LAST_FRAME_UPSCALE_WORKFLOW_NAME}")
        return self.create_job_from_existing_media(
            telegram_user=telegram_user,
            media_asset=input_asset,
            workflow_name=workflow_name,
            prompt="",
            metadata={
                "parsed_instruction": {
                    "workflow": workflow_name,
                    "prompt": "",
                    "seed": 0,
                    "raw_text": "/lastframeupscale",
                },
                "last_frame_upscale": {
                    "source_video_media_asset_id": video_asset.id,
                    "extracted_input_media_asset_id": input_asset.id,
                },
            },
        )

    def combine_latest_videos(self, telegram_user: TelegramUser, count: int = 2) -> MediaAsset | None:
        if count < 2:
            raise ValueError("At least two videos are required for combination.")
        videos = self.list_recent_videos(telegram_user, limit=count)
        if len(videos) < count:
            return None
        ordered_videos = list(reversed(videos))
        return self.combine_videos(telegram_user, ordered_videos)

    def combine_videos_by_ids(self, telegram_user: TelegramUser, media_asset_ids: list[int]) -> MediaAsset | None:
        if len(media_asset_ids) < 2:
            raise ValueError("At least two video ids are required.")
        assets: list[MediaAsset] = []
        for media_asset_id in media_asset_ids:
            asset = self.get_video_media_by_id(telegram_user, media_asset_id)
            if asset is None:
                return None
            assets.append(asset)
        return self.combine_videos(telegram_user, assets)

    def combine_videos_by_job_ids(self, telegram_user: TelegramUser, job_ids: list[int]) -> MediaAsset | None:
        if len(job_ids) < 2:
            raise ValueError("At least two job ids are required.")
        assets: list[MediaAsset] = []
        for job_id in job_ids:
            job = GenerationJob.objects.filter(id=job_id, telegram_user=telegram_user).select_related("output_media").first()
            if job is None or job.output_media is None:
                return None
            asset = job.output_media
            if asset.asset_type not in {MediaAsset.TYPE_INCOMING_VIDEO, MediaAsset.TYPE_GENERATED_VIDEO}:
                return None
            if not self._is_media_asset_available(asset):
                return None
            assets.append(asset)
        return self.combine_videos(telegram_user, assets)

    def combine_videos(self, telegram_user: TelegramUser, video_assets: list[MediaAsset]) -> MediaAsset:
        if len(video_assets) < 2:
            raise ValueError("At least two videos are required for combination.")
        for asset in video_assets:
            if asset.telegram_user_id != telegram_user.id:
                raise ValueError("Media asset does not belong to the requesting Telegram user.")
            if asset.asset_type not in {MediaAsset.TYPE_INCOMING_VIDEO, MediaAsset.TYPE_GENERATED_VIDEO}:
                raise ValueError("Video combination only supports stored video assets.")
            if not self._is_media_asset_available(asset):
                raise ValueError("One or more video assets are not available on disk.")

        roots = [str(Path(settings.MEDIA_ROOT).resolve())]
        source_paths = [Path(asset.file.path) for asset in video_assets]
        output_path = source_paths[-1].with_name(f"combined_{video_assets[0].id}_{video_assets[-1].id}.mp4")
        result = self.video_combination_service.combine_videos(
            video_paths=[str(path) for path in source_paths],
            output_path=str(output_path),
            roots=roots,
        )

        combined_path = Path(result["output_path"])
        video_bytes = combined_path.read_bytes()
        combined_asset = MediaAsset(
            telegram_user=telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name=combined_path.name,
            metadata={
                "derived_from_media_asset_ids": [asset.id for asset in video_assets],
                "source_media_asset_types": [asset.asset_type for asset in video_assets],
                "combination": {
                    "kind": "concat",
                    "input_count": len(video_assets),
                    "source_video_paths": [str(path) for path in source_paths],
                },
                "file_size_bytes": len(video_bytes),
            },
        )
        combined_asset.file.save(combined_path.name, ContentFile(video_bytes), save=False)
        combined_asset.save()
        try:
            combined_path.unlink(missing_ok=True)
        except OSError:
            pass
        return combined_asset

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
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        return self.job_service.create_generation_job(
            telegram_user=telegram_user,
            input_media=media_asset,
            workflow_name=resolved_name,
            prompt=prompt,
            seed=seed,
            priority=priority,
            requested_executor=requested_executor,
            metadata=metadata,
        )

    def create_job_from_prompt(
        self,
        telegram_user: TelegramUser,
        workflow_name: str,
        prompt: str,
        seed: int = 0,
        priority: int | None = None,
        requested_executor: str = GenerationJob.EXECUTOR_LOCAL_GPU,
        metadata: dict | None = None,
    ) -> GenerationJob:
        resolved_name = self.resolve_workflow_name(workflow_name)
        if resolved_name is None:
            raise ValueError(f"Workflow not found: {workflow_name}")
        return self.job_service.create_generation_job(
            telegram_user=telegram_user,
            input_media=None,
            workflow_name=resolved_name,
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
