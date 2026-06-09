from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from django.conf import settings


class WorkflowManager:
    def __init__(self, workflow_dir: str | Path | None = None) -> None:
        self.workflow_dir = Path(workflow_dir or settings.WORKFLOWS_DIR).resolve()

    def list_workflows(self) -> list[str]:
        return sorted(path.stem for path in self.workflow_dir.glob("*.json"))

    def load_workflow(self, name: str) -> dict[str, Any]:
        workflow_path = self._resolve_workflow_path(name)
        with workflow_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def render_workflow(self, name: str, placeholders: dict[str, Any]) -> dict[str, Any]:
        workflow = self.load_workflow(name)
        string_placeholders = {key: str(value) for key, value in placeholders.items()}
        return self._replace_placeholders(workflow, string_placeholders)

    def _resolve_workflow_path(self, name: str) -> Path:
        candidate_name = name if name.endswith(".json") else f"{name}.json"
        candidate_path = (self.workflow_dir / candidate_name).resolve()
        if candidate_path.parent != self.workflow_dir:
            raise ValueError("Workflow path traversal is not allowed")
        if not candidate_path.is_file():
            raise FileNotFoundError(f"Workflow not found: {name}")
        return candidate_path

    def _replace_placeholders(self, value: Any, placeholders: dict[str, str]) -> Any:
        if isinstance(value, dict):
            return {
                key: self._replace_placeholders(item, placeholders)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._replace_placeholders(item, placeholders) for item in value]
        if isinstance(value, str):
            replaced = value
            for placeholder, actual in placeholders.items():
                replaced = replaced.replace(placeholder, actual)
            if replaced.isdigit():
                return int(replaced)
            return replaced
        return copy.deepcopy(value)
