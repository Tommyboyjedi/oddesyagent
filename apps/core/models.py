from django.db import models
from django.utils import timezone


class TelegramUser(models.Model):
    telegram_user_id = models.BigIntegerField(unique=True)
    username = models.CharField(max_length=255, blank=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    is_allowed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        label = self.username or self.first_name or str(self.telegram_user_id)
        return f"{label} ({self.telegram_user_id})"


class MediaAsset(models.Model):
    TYPE_INCOMING_IMAGE = "incoming_image"
    TYPE_GENERATED_IMAGE = "generated_image"
    TYPE_GENERATED_VIDEO = "generated_video"
    TYPE_OTHER = "other"
    ASSET_TYPE_CHOICES = [
        (TYPE_INCOMING_IMAGE, "Incoming image"),
        (TYPE_GENERATED_IMAGE, "Generated image"),
        (TYPE_GENERATED_VIDEO, "Generated video"),
        (TYPE_OTHER, "Other"),
    ]

    file = models.FileField(upload_to="assets/%Y/%m/%d/")
    asset_type = models.CharField(max_length=32, choices=ASSET_TYPE_CHOICES, default=TYPE_OTHER)
    original_file_name = models.CharField(max_length=255, blank=True)
    telegram_file_id = models.CharField(max_length=255, blank=True)
    telegram_user = models.ForeignKey(
        TelegramUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="media_assets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"{self.asset_type}:{self.id}:{self.original_file_name or self.file.name}"


class GenerationJob(models.Model):
    EXECUTOR_LOCAL_GPU = "local_gpu"
    EXECUTOR_CLOUD = "cloud"
    EXECUTOR_CHOICES = [
        (EXECUTOR_LOCAL_GPU, "Local GPU"),
        (EXECUTOR_CLOUD, "Cloud"),
    ]
    STATE_QUEUED = "queued"
    STATE_RUNNING = "running"
    STATE_CANCELLATION_REQUESTED = "cancellation_requested"
    STATE_COMPLETED = "completed"
    STATE_FAILED = "failed"
    STATE_CANCELLED = "cancelled"
    STATE_AWAITING_APPROVAL = "awaiting_approval"
    STATE_CHOICES = [
        (STATE_QUEUED, "Queued"),
        (STATE_RUNNING, "Running"),
        (STATE_CANCELLATION_REQUESTED, "Cancellation requested"),
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
    input_media = models.ForeignKey(
        MediaAsset,
        on_delete=models.PROTECT,
        related_name="input_generation_jobs",
    )
    output_media = models.ForeignKey(
        MediaAsset,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="output_generation_jobs",
    )
    workflow_name = models.CharField(max_length=255, default="i2v_wan_480p")
    prompt = models.TextField(blank=True)
    seed = models.BigIntegerField(default=0)
    priority = models.IntegerField(default=100, db_index=True)
    requested_executor = models.CharField(
        max_length=32,
        choices=EXECUTOR_CHOICES,
        default=EXECUTOR_LOCAL_GPU,
        db_index=True,
    )
    comfyui_prompt_id = models.CharField(max_length=255, blank=True)
    error_message = models.TextField(blank=True)
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=STATE_QUEUED, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"job:{self.id}:{self.state}:p{self.priority}:{self.requested_executor}"

    def mark_running(self) -> None:
        self.state = self.STATE_RUNNING
        self.started_at = timezone.now()
        self.save(update_fields=["state", "started_at", "updated_at"])

    def mark_cancellation_requested(self) -> None:
        self.state = self.STATE_CANCELLATION_REQUESTED
        self.metadata["cancellation_requested_at"] = timezone.now().isoformat()
        self.save(update_fields=["state", "metadata", "updated_at"])

    def mark_completed(self, output_media: MediaAsset | None = None) -> None:
        self.state = self.STATE_COMPLETED
        self.output_media = output_media
        self.completed_at = timezone.now()
        self.error_message = ""
        self.save(
            update_fields=[
                "state",
                "output_media",
                "completed_at",
                "error_message",
                "updated_at",
            ]
        )

    def mark_failed(self, error_message: str) -> None:
        self.state = self.STATE_FAILED
        self.error_message = error_message
        self.completed_at = timezone.now()
        self.save(update_fields=["state", "error_message", "completed_at", "updated_at"])

    def mark_cancelled(self) -> None:
        self.state = self.STATE_CANCELLED
        self.completed_at = timezone.now()
        self.save(update_fields=["state", "completed_at", "updated_at"])


class AuditLog(models.Model):
    event_type = models.CharField(max_length=64)
    telegram_user = models.ForeignKey(
        TelegramUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    generation_job = models.ForeignKey(
        GenerationJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    message = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.created_at.isoformat()}:{self.event_type}:{self.message}"


class ToolDefinition(models.Model):
    name = models.CharField(max_length=128, unique=True)
    description = models.TextField()
    allowed_inputs = models.JSONField(default=list, blank=True)
    forbidden_inputs = models.JSONField(default=list, blank=True)
    audit_requirements = models.JSONField(default=list, blank=True)
    requires_confirmation = models.BooleanField(default=False)
    is_destructive = models.BooleanField(default=False)
    is_external = models.BooleanField(default=False)
    safe_roots = models.JSONField(default=list, blank=True)
    is_enabled = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"tool:{self.name}:{'enabled' if self.is_enabled else 'disabled'}"


class ToolExecutionRequest(models.Model):
    STATUS_PENDING = "pending"
    STATUS_AWAITING_CONFIRMATION = "awaiting_confirmation"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_EXECUTED = "executed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_AWAITING_CONFIRMATION, "Awaiting confirmation"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_EXECUTED, "Executed"),
    ]

    tool = models.ForeignKey(
        ToolDefinition,
        on_delete=models.CASCADE,
        related_name="execution_requests",
    )
    telegram_user = models.ForeignKey(
        TelegramUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tool_execution_requests",
    )
    requested_inputs = models.JSONField(default=dict, blank=True)
    decision_message = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    requires_confirmation = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"tool_request:{self.id}:{self.tool.name}:{self.status}"

    def mark_awaiting_confirmation(self, message: str) -> None:
        self.status = self.STATUS_AWAITING_CONFIRMATION
        self.requires_confirmation = True
        self.decision_message = message
        self.save(update_fields=["status", "requires_confirmation", "decision_message", "updated_at"])

    def mark_approved(self, message: str) -> None:
        self.status = self.STATUS_APPROVED
        self.decision_message = message
        self.confirmed_at = timezone.now()
        self.save(update_fields=["status", "decision_message", "confirmed_at", "updated_at"])

    def mark_rejected(self, message: str) -> None:
        self.status = self.STATUS_REJECTED
        self.decision_message = message
        self.rejected_at = timezone.now()
        self.save(update_fields=["status", "decision_message", "rejected_at", "updated_at"])

    def mark_executed(self, message: str, execution_result: dict | None = None) -> None:
        self.status = self.STATUS_EXECUTED
        self.decision_message = message
        self.executed_at = timezone.now()
        if execution_result is not None:
            self.metadata["execution_result"] = execution_result
        self.save(update_fields=["status", "decision_message", "executed_at", "metadata", "updated_at"])
