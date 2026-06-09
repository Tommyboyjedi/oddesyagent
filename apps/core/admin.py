from django.contrib import admin

from .models import AuditLog, GenerationJob, MediaAsset, TelegramUser, ToolDefinition, ToolExecutionRequest


@admin.register(TelegramUser)
class TelegramUserAdmin(admin.ModelAdmin):
    list_display = ("telegram_user_id", "username", "is_allowed", "created_at", "updated_at")
    search_fields = ("telegram_user_id", "username", "first_name", "last_name")
    list_filter = ("is_allowed",)


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ("id", "asset_type", "telegram_user", "original_file_name", "created_at")
    search_fields = ("original_file_name", "telegram_file_id", "file")
    list_filter = ("asset_type",)


@admin.register(GenerationJob)
class GenerationJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "telegram_user",
        "workflow_name",
        "state",
        "seed",
        "created_at",
        "updated_at",
    )
    search_fields = ("workflow_name", "prompt", "comfyui_prompt_id")
    list_filter = ("state", "workflow_name")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("id", "event_type", "message", "telegram_user", "generation_job", "created_at")
    search_fields = ("event_type", "message")
    list_filter = ("event_type",)


@admin.register(ToolDefinition)
class ToolDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "is_enabled", "requires_confirmation", "is_destructive", "is_external", "updated_at")
    search_fields = ("name", "description")
    list_filter = ("is_enabled", "requires_confirmation", "is_destructive", "is_external")


@admin.register(ToolExecutionRequest)
class ToolExecutionRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "tool", "telegram_user", "status", "requires_confirmation", "created_at", "updated_at")
    search_fields = ("tool__name", "decision_message")
    list_filter = ("status", "requires_confirmation", "tool__name")
