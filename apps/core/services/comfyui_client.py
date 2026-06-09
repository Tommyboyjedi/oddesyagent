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
                return history[prompt_id]
            if time.time() - started_at > timeout_seconds:
                raise ComfyUIClientError(f"Timed out waiting for ComfyUI prompt {prompt_id}")
            time.sleep(poll_seconds)
