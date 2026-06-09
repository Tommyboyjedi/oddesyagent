from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from django.conf import settings


class ComfyUIClient:
    def __init__(self, base_url: str | None = None, timeout: int = 30) -> None:
        self.base_url = (base_url or settings.COMFYUI_BASE_URL).rstrip("/")
        self.timeout = timeout

    def submit_workflow(self, workflow_payload: dict[str, Any]) -> str:
        response = requests.post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow_payload},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ValueError("ComfyUI response missing prompt_id")
        return prompt_id

    def upload_input_image(self, image_path: Path) -> str:
        with image_path.open("rb") as handle:
            response = requests.post(
                f"{self.base_url}/upload/image",
                files={"image": (image_path.name, handle)},
                timeout=self.timeout,
            )
        response.raise_for_status()
        data = response.json()
        uploaded_name = data.get("name")
        if not uploaded_name:
            raise ValueError("ComfyUI upload response missing file name")
        return uploaded_name

    def get_history(self, prompt_id: str) -> dict[str, Any]:
        response = requests.get(
            f"{self.base_url}/history/{prompt_id}",
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def wait_for_completion(
        self,
        prompt_id: str,
        poll_interval: int | None = None,
        timeout_seconds: int = 3600,
    ) -> dict[str, Any]:
        interval = poll_interval or settings.POLL_INTERVAL_SECONDS
        started_at = time.time()
        while True:
            history = self.get_history(prompt_id)
            prompt_data = history.get(prompt_id)
            if prompt_data:
                return prompt_data
            if time.time() - started_at > timeout_seconds:
                raise TimeoutError(f"Timed out waiting for ComfyUI prompt {prompt_id}")
            time.sleep(interval)

    def extract_outputs(self, history_payload: dict[str, Any]) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        history_outputs = history_payload.get("outputs", {})
        for node_id, node_payload in history_outputs.items():
            for key, items in node_payload.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    output = dict(item)
                    output["node_id"] = node_id
                    output["output_key"] = key
                    outputs.append(output)
        return outputs

    def download_output(self, output: dict[str, Any], destination: Path) -> Path:
        filename = output.get("filename")
        if not filename:
            raise ValueError("ComfyUI output is missing filename")
        query = urlencode(
            {
                "filename": filename,
                "subfolder": output.get("subfolder", ""),
                "type": output.get("type", "output"),
            }
        )
        response = requests.get(
            f"{self.base_url}/view?{query}",
            timeout=self.timeout,
        )
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)
        return destination
