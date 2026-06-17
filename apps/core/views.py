from __future__ import annotations

import json
from ipaddress import ip_address

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views import View

from apps.core.models import TelegramUser
from apps.core.services.oddesy_agent_service import OddesyAgentService


class InternalApiValidationError(ValueError):
    pass


class InternalApiBaseView(View):
    def dispatch(self, request: HttpRequest, *args, **kwargs):
        auth_error = self._authorize(request)
        if auth_error is not None:
            return auth_error
        return super().dispatch(request, *args, **kwargs)

    @property
    def oddesy_agent_service(self) -> OddesyAgentService:
        return OddesyAgentService()

    def _authorize(self, request: HttpRequest):
        if not settings.ODDESY_INTERNAL_API_ENABLED:
            return JsonResponse({"detail": "Internal API is disabled."}, status=404)
        if not self._is_loopback_request(request):
            return JsonResponse({"detail": "Internal API is loopback-only."}, status=403)
        auth_header = request.headers.get("Authorization", "")
        expected = settings.ODDESY_INTERNAL_API_TOKEN
        if not expected or auth_header != f"Bearer {expected}":
            return JsonResponse({"detail": "Invalid internal API token."}, status=401)
        return None

    def _is_loopback_request(self, request: HttpRequest) -> bool:
        remote_addr = request.META.get("REMOTE_ADDR", "")
        if not remote_addr:
            return False
        try:
            return ip_address(remote_addr).is_loopback
        except ValueError:
            return False

    def _get_telegram_user(self, telegram_user_id: int) -> TelegramUser | None:
        return TelegramUser.objects.filter(telegram_user_id=telegram_user_id, is_allowed=True).first()

    def _json_body(self, request: HttpRequest) -> dict:
        if not request.body:
            return {}
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise InternalApiValidationError("Request body must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise InternalApiValidationError("Request body must decode to a JSON object.")
        return payload

    def _parse_int(self, raw_value, *, field_name: str, minimum: int | None = None) -> int:
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise InternalApiValidationError(f"Field '{field_name}' must be an integer.") from exc
        if minimum is not None and value < minimum:
            raise InternalApiValidationError(f"Field '{field_name}' must be >= {minimum}.")
        return value

    def _parse_optional_int(self, raw_value, *, field_name: str, default: int = 0, minimum: int | None = None) -> int:
        if raw_value in [None, ""]:
            return default
        return self._parse_int(raw_value, field_name=field_name, minimum=minimum)


class InternalWorkflowsView(InternalApiBaseView):
    def get(self, request: HttpRequest):
        return JsonResponse({"workflows": self.oddesy_agent_service.list_workflows()})


class InternalJobsView(InternalApiBaseView):
    def post(self, request: HttpRequest):
        try:
            payload = self._json_body(request)
            telegram_user_id = self._parse_int(payload.get("telegram_user_id", 0), field_name="telegram_user_id", minimum=1)
            media_asset_id = self._parse_int(payload.get("input_media_id", 0), field_name="input_media_id", minimum=1)
            seed = self._parse_optional_int(payload.get("seed", 0), field_name="seed", default=0, minimum=0)
        except InternalApiValidationError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)

        telegram_user = self._get_telegram_user(telegram_user_id)
        if telegram_user is None:
            return JsonResponse({"detail": "Allowed Telegram user not found."}, status=404)

        media_asset = telegram_user.media_assets.filter(id=media_asset_id).first()
        if media_asset is None:
            return JsonResponse({"detail": "Input media not found for Telegram user."}, status=404)

        workflow_name = payload.get("workflow_name") or settings.DEFAULT_WORKFLOW_NAME
        prompt = payload.get("prompt", "")
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            return JsonResponse({"detail": "Field 'metadata' must be a JSON object."}, status=400)
        try:
            job = self.oddesy_agent_service.create_job_from_existing_media(
                telegram_user=telegram_user,
                media_asset=media_asset,
                workflow_name=workflow_name,
                prompt=prompt,
                seed=seed,
                metadata=metadata,
            )
        except ValueError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        self.oddesy_agent_service.job_service.log_job_event(
            job,
            "job_created",
            "queued",
            {"job_id": job.id, "input_media_id": media_asset.id, "source": "internal_api"},
        )
        return JsonResponse(self.oddesy_agent_service.get_job_status_payload(telegram_user, job.id), status=201)


class InternalJobDetailView(InternalApiBaseView):
    def get(self, request: HttpRequest, job_id: int):
        try:
            telegram_user_id = self._parse_int(request.GET.get("telegram_user_id", "0"), field_name="telegram_user_id", minimum=1)
        except InternalApiValidationError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        telegram_user = self._get_telegram_user(telegram_user_id)
        if telegram_user is None:
            return JsonResponse({"detail": "Allowed Telegram user not found."}, status=404)
        payload = self.oddesy_agent_service.get_job_status_payload(telegram_user, job_id)
        if payload is None:
            return JsonResponse({"detail": "Job not found."}, status=404)
        return JsonResponse(payload)


class InternalMediaListView(InternalApiBaseView):
    def get(self, request: HttpRequest):
        try:
            telegram_user_id = self._parse_int(request.GET.get("telegram_user_id", "0"), field_name="telegram_user_id", minimum=1)
            limit = self._parse_optional_int(request.GET.get("limit", "20"), field_name="limit", default=20, minimum=1)
        except InternalApiValidationError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        telegram_user = self._get_telegram_user(telegram_user_id)
        if telegram_user is None:
            return JsonResponse({"detail": "Allowed Telegram user not found."}, status=404)
        asset_types = request.GET.getlist("asset_type")
        payloads = self.oddesy_agent_service.list_media_payloads(
            telegram_user,
            asset_types=asset_types or None,
            limit=limit,
        )
        return JsonResponse({"media": payloads})


class InternalJobOutputView(InternalApiBaseView):
    def get(self, request: HttpRequest, job_id: int):
        try:
            telegram_user_id = self._parse_int(request.GET.get("telegram_user_id", "0"), field_name="telegram_user_id", minimum=1)
        except InternalApiValidationError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        telegram_user = self._get_telegram_user(telegram_user_id)
        if telegram_user is None:
            return JsonResponse({"detail": "Allowed Telegram user not found."}, status=404)
        payload = self.oddesy_agent_service.get_generated_output_payload(telegram_user, job_id)
        if payload is None:
            return JsonResponse({"detail": "Generated output not found."}, status=404)
        return JsonResponse(payload)
