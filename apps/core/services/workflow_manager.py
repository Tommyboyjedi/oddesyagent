from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

from django.conf import settings


class WorkflowManager:
    def __init__(self, workflow_path: str | None = None) -> None:
        self.workflow_path = Path(workflow_path or settings.COMFYUI_WORKFLOW_PATH)

    def list_workflows(self) -> list[str]:
        workflow_dir = self.workflow_path.parent
        return sorted(path.stem for path in workflow_dir.glob("*.json"))

    def load_template(self) -> dict[str, Any]:
        with self.workflow_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def build_workflow(
        self,
        input_image: str,
        prompt: str,
        seed: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        resolved_seed = seed if seed is not None else random.randint(1, 2**31 - 1)
        template = self.load_template()
        replacements = {
            "{INPUT_IMAGE}": input_image,
            "{PROMPT}": prompt,
            "{SEED}": str(resolved_seed),
        }
        return self._replace_placeholders(template, replacements), resolved_seed

    def _replace_placeholders(
        self,
        value: Any,
        replacements: dict[str, str],
    ) -> Any:
        if isinstance(value, dict):
            return {key: self._replace_placeholders(item, replacements) for key, item in value.items()}
        if isinstance(value, list):
            return [self._replace_placeholders(item, replacements) for item in value]
        if isinstance(value, str):
            replaced = value
            for placeholder, actual in replacements.items():
                replaced = replaced.replace(placeholder, actual)
            if replaced.isdigit():
                return int(replaced)
            return replaced
        return copy.deepcopy(value)
