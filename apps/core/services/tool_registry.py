from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings

from apps.core.models import AuditLog, TelegramUser, ToolDefinition, ToolExecutionRequest
from apps.core.services.media_cleanup_executor import MediaCleanupExecutorService
from apps.core.services.media_library_cleanup import MediaLibraryCleanupService
from apps.core.services.media_library_report import MediaLibraryReportService
from apps.core.services.media_cleanup_preview import MediaCleanupPreviewService
from apps.core.services.safe_root_browser import SafeRootBrowserError, SafeRootBrowserService
from apps.core.services.video_last_frame_enhancement import VideoLastFrameEnhancementService


@dataclass(frozen=True)
class ToolDecision:
    status: str
    message: str
    tool_name: str | None = None
    requires_confirmation: bool = False


class ToolRegistryService:
    URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)
    WILDCARD_PATTERN = re.compile(r"[*?]")
    SHELL_PATTERN = re.compile(
        r"\b(powershell|cmd\.exe|bash|zsh|sh|curl|wget|rm\s+-rf|python\s+-c)\b",
        re.IGNORECASE,
    )
    FILE_PATH_PATTERN = re.compile(r"([A-Za-z]:\\|(?:^|[\\/])[^\\/\s]+(?:[\\/][^\\/\s]+)+)")

    def register_tool(
        self,
        *,
        name: str,
        description: str,
        allowed_inputs: list[str],
        forbidden_inputs: list[str],
        audit_requirements: list[str],
        requires_confirmation: bool = False,
        is_destructive: bool = False,
        is_external: bool = False,
        safe_roots: list[str] | None = None,
        is_enabled: bool = False,
        metadata: dict | None = None,
    ) -> ToolDefinition:
        tool, _ = ToolDefinition.objects.update_or_create(
            name=name,
            defaults={
                "description": description,
                "allowed_inputs": allowed_inputs,
                "forbidden_inputs": forbidden_inputs,
                "audit_requirements": audit_requirements,
                "requires_confirmation": requires_confirmation,
                "is_destructive": is_destructive,
                "is_external": is_external,
                "safe_roots": safe_roots or [],
                "is_enabled": is_enabled,
                "metadata": metadata or {},
            },
        )
        return tool

    def list_enabled_tools(self) -> list[ToolDefinition]:
        return list(ToolDefinition.objects.filter(is_enabled=True).order_by("name"))

    def list_requests(self, status: str | None = None, limit: int = 20) -> list[ToolExecutionRequest]:
        queryset = ToolExecutionRequest.objects.select_related("tool", "telegram_user").order_by("-created_at", "-id")
        if status:
            queryset = queryset.filter(status=status)
        return list(queryset[:limit])

    def execute_request(self, request_id: int) -> ToolExecutionRequest:
        request = ToolExecutionRequest.objects.select_related("tool", "telegram_user").get(id=request_id)
        if request.status != ToolExecutionRequest.STATUS_APPROVED:
            raise ValueError(f"Tool request #{request.id} is not approved for execution.")

        executor_name = str(request.tool.metadata.get("executor", "")).lower()
        if executor_name == "safe_root_browser":
            browser = SafeRootBrowserService()
            execution_result = browser.browse(
                target_path=str(request.requested_inputs.get("target_path", "")),
                recursive=bool(request.requested_inputs.get("recursive", False)),
                limit=int(request.requested_inputs.get("limit", 100)),
                roots=request.tool.safe_roots,
            )
        elif executor_name == "media_cleanup_preview":
            preview = MediaCleanupPreviewService()
            execution_result = preview.preview(
                target_path=str(request.requested_inputs.get("target_path", "")),
                recursive=bool(request.requested_inputs.get("recursive", True)),
                limit=int(request.requested_inputs.get("limit", 100)),
                older_than_days=int(request.requested_inputs.get("older_than_days", 0)),
                extensions=list(request.requested_inputs.get("extensions", [])),
                roots=request.tool.safe_roots,
            )
        elif executor_name == "media_cleanup":
            cleanup = MediaCleanupExecutorService()
            execution_result = cleanup.execute(
                target_path=str(request.requested_inputs.get("target_path", "")),
                recursive=bool(request.requested_inputs.get("recursive", True)),
                limit=int(request.requested_inputs.get("limit", 100)),
                older_than_days=int(request.requested_inputs.get("older_than_days", 0)),
                extensions=list(request.requested_inputs.get("extensions", [])),
                roots=request.tool.safe_roots,
            )
        elif executor_name == "media_library_report":
            report = MediaLibraryReportService()
            execution_result = report.generated_media_cleanup_report(
                older_than_days=int(request.requested_inputs.get("older_than_days", 0)),
                asset_types=list(request.requested_inputs.get("asset_types", [])) or None,
                include_missing_files=bool(request.requested_inputs.get("include_missing_files", True)),
                limit=int(request.requested_inputs.get("limit", 100)),
                safe_roots=request.tool.safe_roots,
            )
        elif executor_name == "media_library_cleanup":
            cleanup = MediaLibraryCleanupService()
            execution_result = cleanup.execute(
                older_than_days=int(request.requested_inputs.get("older_than_days", 0)),
                asset_types=list(request.requested_inputs.get("asset_types", [])) or None,
                include_missing_files=bool(request.requested_inputs.get("include_missing_files", True)),
                limit=int(request.requested_inputs.get("limit", 100)),
                safe_roots=request.tool.safe_roots,
            )
        elif executor_name == "video_last_frame_enhancement":
            enhancer = VideoLastFrameEnhancementService()
            execution_result = enhancer.enhance_last_frame(
                video_path=str(request.requested_inputs.get("video_path", "")),
                output_path=str(request.requested_inputs.get("output_path", "")).strip() or None,
                upscale_factor=float(request.requested_inputs.get("upscale_factor", 2.0)),
                sharpen_amount=float(request.requested_inputs.get("sharpen_amount", 0.4)),
                roots=request.tool.safe_roots,
            )
        else:
            raise ValueError(f"Tool '{request.tool.name}' has no executable handler.")

        request.mark_executed(f"Tool '{request.tool.name}' executed successfully.", execution_result)
        AuditLog.objects.create(
            event_type="tool_registry",
            telegram_user=request.telegram_user,
            message="executed",
            metadata={"tool_request_id": request.id, "tool_name": request.tool.name},
        )
        return request

    def evaluate_request(self, tool_name: str, requested_inputs: dict[str, Any]) -> ToolDecision:
        tool = ToolDefinition.objects.filter(name=tool_name).first()
        if tool is None:
            return ToolDecision(status="rejected", message=f"Unknown tool '{tool_name}'.")
        if not tool.is_enabled:
            return ToolDecision(status="rejected", message=f"Tool '{tool.name}' is disabled.", tool_name=tool.name)

        requested_keys = set(requested_inputs.keys())
        unexpected_keys = sorted(requested_keys - set(tool.allowed_inputs))
        if unexpected_keys:
            return ToolDecision(
                status="rejected",
                message=f"Tool '{tool.name}' does not allow inputs: {', '.join(unexpected_keys)}.",
                tool_name=tool.name,
            )

        forbidden_keys = sorted(requested_keys.intersection(tool.forbidden_inputs))
        if forbidden_keys:
            return ToolDecision(
                status="rejected",
                message=f"Tool '{tool.name}' forbids inputs: {', '.join(forbidden_keys)}.",
                tool_name=tool.name,
            )

        unsafe_error = self._get_unsafe_input_error(tool, requested_inputs)
        if unsafe_error is not None:
            return ToolDecision(status="rejected", message=unsafe_error, tool_name=tool.name)

        configuration_error = self._get_configuration_error(tool)
        if configuration_error is not None:
            return ToolDecision(status="rejected", message=configuration_error, tool_name=tool.name)

        requires_confirmation = tool.requires_confirmation or tool.is_destructive or tool.is_external
        if requires_confirmation:
            return ToolDecision(
                status="awaiting_confirmation",
                message=f"Tool '{tool.name}' requires explicit confirmation before execution.",
                tool_name=tool.name,
                requires_confirmation=True,
            )
        return ToolDecision(status="approved", message=f"Tool '{tool.name}' is allowed.", tool_name=tool.name)

    def submit_request(
        self,
        *,
        tool_name: str,
        requested_inputs: dict[str, Any],
        telegram_user: TelegramUser | None = None,
    ) -> ToolExecutionRequest:
        tool = ToolDefinition.objects.filter(name=tool_name).first()
        if tool is None:
            raise ValueError(f"Unknown tool '{tool_name}'.")

        decision = self.evaluate_request(tool_name, requested_inputs)
        request = ToolExecutionRequest.objects.create(
            tool=tool,
            telegram_user=telegram_user,
            requested_inputs=requested_inputs,
            decision_message=decision.message,
            status=ToolExecutionRequest.STATUS_PENDING,
            requires_confirmation=decision.requires_confirmation,
            metadata={
                "decision": decision.status,
                "tool_name": tool_name,
            },
        )

        if decision.status == "approved":
            request.mark_approved(decision.message)
        elif decision.status == "awaiting_confirmation":
            request.mark_awaiting_confirmation(decision.message)
        else:
            request.mark_rejected(decision.message)

        self.log_tool_decision(
            tool_name=tool_name,
            decision=decision,
            requested_inputs=requested_inputs,
            telegram_user=telegram_user,
        )
        return request

    def confirm_request(self, request_id: int) -> ToolExecutionRequest:
        request = ToolExecutionRequest.objects.select_related("tool").get(id=request_id)
        if request.status != ToolExecutionRequest.STATUS_AWAITING_CONFIRMATION:
            raise ValueError(f"Tool request #{request.id} is not awaiting confirmation.")
        request.mark_approved(f"Tool '{request.tool.name}' was explicitly confirmed.")
        AuditLog.objects.create(
            event_type="tool_registry",
            telegram_user=request.telegram_user,
            message="confirmed",
            metadata={"tool_request_id": request.id, "tool_name": request.tool.name},
        )
        return request

    def reject_request(self, request_id: int, reason: str | None = None) -> ToolExecutionRequest:
        request = ToolExecutionRequest.objects.select_related("tool").get(id=request_id)
        if request.status not in [
            ToolExecutionRequest.STATUS_AWAITING_CONFIRMATION,
            ToolExecutionRequest.STATUS_PENDING,
        ]:
            raise ValueError(f"Tool request #{request.id} cannot be rejected from state '{request.status}'.")
        message = reason or f"Tool '{request.tool.name}' request was rejected."
        request.mark_rejected(message)
        AuditLog.objects.create(
            event_type="tool_registry",
            telegram_user=request.telegram_user,
            message="rejected",
            metadata={"tool_request_id": request.id, "tool_name": request.tool.name, "reason": message},
        )
        return request

    def log_tool_decision(
        self,
        *,
        tool_name: str,
        decision: ToolDecision,
        requested_inputs: dict[str, Any],
        telegram_user: TelegramUser | None = None,
    ) -> AuditLog:
        return AuditLog.objects.create(
            event_type="tool_registry",
            telegram_user=telegram_user,
            message=decision.status,
            metadata={
                "tool_name": tool_name,
                "decision": decision.status,
                "message": decision.message,
                "requested_inputs": requested_inputs,
            },
        )

    def _get_unsafe_input_error(self, tool: ToolDefinition, requested_inputs: dict[str, Any]) -> str | None:
        safe_roots = [Path(root).resolve() for root in (tool.safe_roots or settings.ODDESY_SAFE_ROOTS) if root]
        for key, value in requested_inputs.items():
            for string_value in self._iter_string_values(value):
                if self.URL_PATTERN.search(string_value):
                    return f"Tool '{tool.name}' rejects URL inputs."
                if self.WILDCARD_PATTERN.search(string_value):
                    return f"Tool '{tool.name}' rejects wildcard inputs."
                if self.SHELL_PATTERN.search(string_value):
                    return f"Tool '{tool.name}' rejects shell-like inputs."
                if self.FILE_PATH_PATTERN.search(string_value):
                    if not safe_roots:
                        return f"Tool '{tool.name}' does not allow filesystem paths without configured safe roots."
                    if not self._path_is_within_safe_roots(string_value, safe_roots):
                        return f"Tool '{tool.name}' only allows filesystem paths inside configured safe roots."
        return None

    def _get_configuration_error(self, tool: ToolDefinition) -> str | None:
        provider = str(tool.metadata.get("provider", "")).lower()
        if provider == "vast" and not settings.VAST_API_KEY:
            return "Vast.ai access is not configured."
        if provider == "youtube" and not settings.YOUTUBE_UPLOAD_ENABLED:
            return "YouTube upload support is disabled."
        if provider == "nas" and not (tool.safe_roots or settings.ODDESY_SAFE_ROOTS):
            return "NAS/file tools require configured safe roots."
        return None

    def _iter_string_values(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            values: list[str] = []
            for item in value.values():
                values.extend(self._iter_string_values(item))
            return values
        if isinstance(value, (list, tuple)):
            values: list[str] = []
            for item in value:
                values.extend(self._iter_string_values(item))
            return values
        return []

    def _path_is_within_safe_roots(self, raw_path: str, safe_roots: list[Path]) -> bool:
        try:
            candidate = Path(raw_path).resolve()
        except OSError:
            return False
        return any(candidate.is_relative_to(root) for root in safe_roots)
