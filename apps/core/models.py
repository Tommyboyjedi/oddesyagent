from django.db import models
from django.utils import timezone


class TelegramUser(models.Model):
    telegram_id = models.BigIntegerField(unique=True)
    username = models.CharField(max_length=255, blank=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    is_allowed = models.BooleanField(default=False)
    last_seen_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        label = self.username or self.first_name or str(self.telegram_id)
        return f"{label} ({self.telegram_id})"


class MediaAsset(models.Model):
    KIND_IMAGE = "image"
    KIND_VIDEO = "video"
    KIND_OTHER = "other"
    KIND_CHOICES = [
        (KIND_IMAGE, "Image"),
        (KIND_VIDEO, "Video"),
        (KIND_OTHER, "Other"),
    ]

    SOURCE_TELEGRAM = "telegram"
    SOURCE_COMFYUI = "comfyui"
    SOURCE_LOCAL = "local"
    SOURCE_CHOICES = [
        (SOURCE_TELEGRAM, "Telegram"),
        (SOURCE_COMFYUI, "ComfyUI"),
        (SOURCE_LOCAL, "Local"),
    ]

    telegram_user = models.ForeignKey(
        TelegramUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="media_assets",
    )
    file = models.FileField(upload_to="assets/%Y/%m/%d/")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_OTHER)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_LOCAL)
    original_name = models.CharField(max_length=255, blank=True)
    prompt_text = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}:{self.original_name or self.file.name}"


class GenerationJob(models.Model):
    STATE_QUEUED = "queued"
    STATE_RUNNING = "running"
    STATE_COMPLETED = "completed"
    STATE_FAILED = "failed"
    STATE_CANCELLED = "cancelled"
    STATE_AWAITING_APPROVAL = "awaiting_approval"
    STATE_CHOICES = [
        (STATE_QUEUED, "Queued"),
        (STATE_RUNNING, "Running"),
        (STATE_COMPLETED, "Completed"),
        (STATE_FAILED, "Failed"),
        (STATE_CANCELLED, "Cancelled"),
        (STATE_AWAITING_APPROVAL, "Awaiting approval"),
    ]

    telegram_user = models.ForeignKey(
        TelegramUser,
        on_delete=models.CASCADE,
        related_name="generation_jobs",
    )
    input_asset = models.ForeignKey(
        MediaAsset,
        on_delete=models.PROTECT,
        related_name="input_jobs",
    )
    output_asset = models.ForeignKey(
        MediaAsset,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="output_jobs",
    )
    workflow_name = models.CharField(max_length=255, default="i2v_wan_480p")
    prompt_text = models.TextField(blank=True)
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=STATE_QUEUED)
    seed = models.BigIntegerField(default=0)
    external_prompt_id = models.CharField(max_length=255, blank=True)
    result_payload = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"job:{self.id}:{self.state}"


class AuditLog(models.Model):
    telegram_user = models.ForeignKey(
        TelegramUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    event_type = models.CharField(max_length=64)
    message = models.CharField(max_length=255)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.created_at.isoformat()}:{self.event_type}:{self.message}"
