from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass

from django.conf import settings

from apps.core.services.workflow_manager import WorkflowManager


@dataclass(frozen=True)
class ParsedIntent:
    action: str
    workflow_name: str | None = None
    prompt: str | None = None
    seed: int = 0
    duration: int | None = None
    motion: str | None = None
    job_id: int | None = None
    needs_confirmation: bool = False
    message: str = ""
    metadata: dict | None = None


class InstructionParserService:
    ALLOWED_ACTIONS = {"create_job", "rerun", "status", "queue", "unknown"}
    URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)
    FILE_PATH_PATTERN = re.compile(r"([A-Za-z]:\\|(?:^|\s)\.\.?[\\/]|(?:^|\s)/\S+)")
    SHELL_PATTERN = re.compile(
        r"\b(powershell|cmd\.exe|bash|zsh|sh|curl|wget|rm\s+-rf|python\s+-c)\b",
        re.IGNORECASE,
    )
    DURATION_PATTERN = re.compile(r"\b(\d+)\s*(?:second|seconds|sec|s)\b", re.IGNORECASE)

    def __init__(
        self,
        workflow_manager: WorkflowManager | None = None,
        litellm_enabled: bool | None = None,
        litellm_model: str | None = None,
        litellm_api_key: str | None = None,
        default_workflow_name: str | None = None,
    ) -> None:
        self.workflow_manager = workflow_manager or WorkflowManager()
        self.litellm_enabled = settings.LITELLM_ENABLED if litellm_enabled is None else litellm_enabled
        self.litellm_model = settings.LITELLM_MODEL if litellm_model is None else (litellm_model or "")
        self.litellm_api_key = settings.LITELLM_API_KEY if litellm_api_key is None else (litellm_api_key or "")
        self.default_workflow_name = default_workflow_name or settings.DEFAULT_WORKFLOW_NAME

    def parse_text(self, text: str) -> ParsedIntent:
        normalized_text = " ".join(text.strip().split())
        if not normalized_text:
            return self._unknown("Send an image, then describe the video you want.")

        fallback_intent = self._parse_fallback_command(normalized_text)
        if fallback_intent is not None:
            return fallback_intent

        if self._contains_unsafe_content(normalized_text):
            return self._unknown("Unsafe instructions are not supported.")

        if not self.litellm_enabled:
            return self._unknown(
                "Unknown input. Send an image, then send 'make video', 'status', 'queue', or 'rerun'."
            )

        return self._parse_with_litellm(normalized_text)

    def _parse_fallback_command(self, text: str) -> ParsedIntent | None:
        lowered = text.lower()
        if lowered == "make video":
            return self._build_create_job_intent(
                workflow_name=self.default_workflow_name,
                prompt="make video",
                source="fallback",
                raw_text=text,
            )
        if lowered == "status":
            return ParsedIntent(action="status", message="status", metadata={"parser": "fallback"})
        if lowered == "queue":
            return ParsedIntent(action="queue", message="queue", metadata={"parser": "fallback"})
        if lowered.startswith("rerun"):
            parts = lowered.split()
            job_id = None
            if len(parts) > 1:
                try:
                    job_id = int(parts[1])
                except ValueError:
                    return self._unknown("Rerun accepts an optional numeric job ID only.")
            return ParsedIntent(
                action="rerun",
                job_id=job_id,
                message="rerun",
                metadata={"parser": "fallback"},
            )
        return None

    def _parse_with_litellm(self, text: str) -> ParsedIntent:
        allowed_workflows = self.workflow_manager.list_workflows()
        if not allowed_workflows:
            return self._unknown("No workflows are available.")
        if not self.litellm_model:
            return self._unknown("LITELLM_MODEL is required when LITELLM_ENABLED is true.")

        try:
            litellm = importlib.import_module("litellm")
        except ImportError:
            return self._unknown("LiteLLM is not installed. Install dependencies before enabling it.")

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a strict intent parser for a local Django Telegram bot. "
                    "Return JSON only with keys action, workflow, prompt, seed, duration, motion, "
                    "job_id, needs_confirmation, and message. "
                    "Allowed actions: create_job, rerun, status, queue, unknown. "
                    "Allowed workflows only: " + ", ".join(allowed_workflows) + ". "
                    "Never return file paths, shell commands, URLs, or unsafe tool requests."
                ),
            },
            {
                "role": "user",
                "content": text,
            },
        ]
        kwargs = {
            "model": self.litellm_model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if self.litellm_api_key:
            kwargs["api_key"] = self.litellm_api_key

        try:
            response = litellm.completion(**kwargs)
            content = response["choices"][0]["message"]["content"]
            payload = json.loads(content)
        except Exception:
            return self._unknown("Could not parse that instruction safely.")

        return self._sanitize_litellm_payload(text, payload, allowed_workflows)

    def _sanitize_litellm_payload(self, raw_text: str, payload: dict, allowed_workflows: list[str]) -> ParsedIntent:
        action = str(payload.get("action", "unknown")).strip().lower()
        if action not in self.ALLOWED_ACTIONS:
            return self._unknown("Could not parse that instruction safely.")

        message = str(payload.get("message", "")).strip()
        if self._contains_unsafe_content(message):
            return self._unknown("Unsafe instructions are not supported.")

        if action in {"status", "queue"}:
            return ParsedIntent(action=action, message=message or action, metadata={"parser": "litellm"})

        if action == "rerun":
            job_id = payload.get("job_id")
            if job_id is not None:
                try:
                    job_id = int(job_id)
                except (TypeError, ValueError):
                    return self._unknown("Could not parse that rerun request safely.")
            return ParsedIntent(action="rerun", job_id=job_id, message=message or "rerun", metadata={"parser": "litellm"})

        if action == "create_job":
            workflow_name = str(payload.get("workflow") or self.default_workflow_name).strip()
            if workflow_name not in allowed_workflows:
                return self._unknown("That workflow is not allowed.")

            prompt = str(payload.get("prompt") or raw_text).strip()
            if not prompt or self._contains_unsafe_content(prompt):
                return self._unknown("Unsafe instructions are not supported.")

            needs_confirmation = bool(payload.get("needs_confirmation", False))
            if needs_confirmation:
                return ParsedIntent(
                    action="unknown",
                    needs_confirmation=True,
                    message=message or "Please clarify the video instruction before I queue it.",
                    metadata={"parser": "litellm"},
                )

            seed = self._coerce_int(payload.get("seed"), default=0)
            duration = self._coerce_optional_int(payload.get("duration"))
            motion = self._coerce_optional_str(payload.get("motion"))
            return self._build_create_job_intent(
                workflow_name=workflow_name,
                prompt=prompt,
                seed=seed,
                duration=duration,
                motion=motion,
                source="litellm",
                raw_text=raw_text,
            )

        return self._unknown(message or "Unknown input.")

    def _build_create_job_intent(
        self,
        workflow_name: str,
        prompt: str,
        seed: int = 0,
        duration: int | None = None,
        motion: str | None = None,
        source: str = "fallback",
        raw_text: str = "",
    ) -> ParsedIntent:
        metadata = {
            "parser": source,
            "parsed_instruction": {
                "workflow": workflow_name,
                "prompt": prompt,
                "seed": seed,
                "duration": duration,
                "motion": motion,
                "raw_text": raw_text,
            },
        }
        return ParsedIntent(
            action="create_job",
            workflow_name=workflow_name,
            prompt=prompt,
            seed=seed,
            duration=duration,
            motion=motion,
            message="create_job",
            metadata=metadata,
        )

    def _contains_unsafe_content(self, value: str) -> bool:
        return bool(
            self.URL_PATTERN.search(value)
            or self.FILE_PATH_PATTERN.search(value)
            or self.SHELL_PATTERN.search(value)
        )

    def _coerce_int(self, value: object, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _coerce_optional_int(self, value: object) -> int | None:
        try:
            if value in (None, ""):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _coerce_optional_str(self, value: object) -> str | None:
        if value in (None, ""):
            return None
        return str(value).strip() or None

    def _unknown(self, message: str) -> ParsedIntent:
        return ParsedIntent(action="unknown", message=message, metadata={"parser": "fallback"})
