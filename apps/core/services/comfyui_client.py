from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from django.conf import settings


class ComfyUIClientError(Exception):
    pass


class ComfyUIClient:
    def __init__(self, base_url: str | None = None, timeout: int = 30) -> None:
        self.base_url = (base_url or settings.COMFYUI_BASE_URL).rstrip("/")
        self.timeout = timeout

    def upload_input_image(self, image_path: Path) -> str:
        try:
            with image_path.open("rb") as handle:
                response = requests.post(
                    f"{self.base_url}/upload/image",
                    files={"image": (image_path.name, handle)},
                    timeout=self.timeout,
                )
            response.raise_for_status()
            data = response.json()
        except (OSError, requests.RequestException, ValueError) as exc:
            raise ComfyUIClientError(f"Failed to upload input image: {exc}") from exc

        uploaded_name = data.get("name")
        if not uploaded_name:
            raise ComfyUIClientError("ComfyUI upload response missing file name")
        return uploaded_name

    def submit_workflow(self, workflow: dict[str, Any]) -> str:
        try:
            response = requests.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except requests.HTTPError as exc:
            response_text = ""
            if exc.response is not None:
                response_text = exc.response.text[:4000]
            detail = f"{exc}"
            if response_text:
                detail = f"{detail} | response={response_text}"
            raise ComfyUIClientError(f"Failed to submit workflow: {detail}") from exc
        except (requests.RequestException, ValueError) as exc:
            raise ComfyUIClientError(f"Failed to submit workflow: {exc}") from exc

        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ComfyUIClientError("ComfyUI response missing prompt_id")
        return prompt_id

    def get_history(self, prompt_id: str) -> dict[str, Any]:
        try:
            response = requests.get(
                f"{self.base_url}/history/{prompt_id}",
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ComfyUIClientError(f"Failed to fetch history for {prompt_id}: {exc}") from exc

    def get_outputs_from_history(self, prompt_id: str) -> list[dict[str, Any]]:
        history = self.get_history(prompt_id)
        prompt_history = history.get(prompt_id, {})
        return self.extract_outputs_from_prompt_history(prompt_history)

    def extract_outputs_from_prompt_history(self, prompt_history: dict[str, Any]) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        for node_id, node_payload in prompt_history.get("outputs", {}).items():
            for output_key, items in node_payload.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict):
                        outputs.append(
                            {
                                **item,
                                "node_id": node_id,
                                "output_key": output_key,
                            }
                        )
        return outputs

    def extract_execution_error_from_prompt_history(self, prompt_history: dict[str, Any]) -> str | None:
        status = prompt_history.get("status", {})
        messages = status.get("messages", [])
        for message in messages:
            if not isinstance(message, list) or len(message) < 2:
                continue
            message_type, payload = message[0], message[1]
            if message_type != "execution_error" or not isinstance(payload, dict):
                continue
            node_type = payload.get("node_type") or "unknown"
            node_id = payload.get("node_id") or "unknown"
            exception_message = str(payload.get("exception_message") or "Unknown ComfyUI execution error").strip()
            return f"{exception_message} (node {node_id}:{node_type})"
        return None

    def download_output_file(
        self,
        filename: str,
        subfolder: str = "",
        output_type: str = "output",
    ) -> bytes:
        query = urlencode(
            {
                "filename": filename,
                "subfolder": subfolder,
                "type": output_type,
            }
        )
        try:
            response = requests.get(
                f"{self.base_url}/view?{query}",
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            raise ComfyUIClientError(f"Failed to download output file {filename}: {exc}") from exc

    def wait_for_completion(
        self,
        prompt_id: str,
        poll_seconds: int,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        started_at = time.time()
        while True:
            history = self.get_history(prompt_id)
            if prompt_id in history:
                prompt_history = history[prompt_id]
                status = prompt_history.get("status", {})
                if status.get("completed") is True:
                    return prompt_history
                if str(status.get("status_str", "")).lower() == "error":
                    return prompt_history
                if prompt_history.get("outputs") and not status:
                    return prompt_history
            if time.time() - started_at > timeout_seconds:
                raise ComfyUIClientError(f"Timed out waiting for ComfyUI prompt {prompt_id}")
            time.sleep(poll_seconds)
