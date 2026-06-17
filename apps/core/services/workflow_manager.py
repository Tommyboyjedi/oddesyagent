from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Callable

from django.conf import settings
import requests


class WorkflowManager:
    KNOWN_PLACEHOLDERS = ("{INPUT_IMAGE}", "{PROMPT}", "{NEGATIVE_PROMPT}", "{LENGTH}", "{SEED}")

    def __init__(
        self,
        workflow_dir: str | Path | None = None,
        object_info_loader: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.workflow_dir = Path(workflow_dir or settings.WORKFLOWS_DIR).resolve()
        self.object_info_loader = object_info_loader
        self._object_info_cache: dict[str, Any] | None = None

    def list_workflows(self) -> list[str]:
        return sorted(path.stem for path in self.workflow_dir.glob("*.json"))

    def load_workflow(self, name: str) -> dict[str, Any]:
        workflow_path = self._resolve_workflow_path(name)
        with workflow_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def render_workflow(self, name: str, placeholders: dict[str, Any]) -> dict[str, Any]:
        workflow = self.load_workflow(name)
        string_placeholders = {key: str(value) for key, value in placeholders.items()}
        rendered = self._replace_placeholders(workflow, string_placeholders)
        missing_placeholders = self._find_unresolved_placeholders(rendered, list(placeholders.keys()))
        if missing_placeholders:
            missing_list = ", ".join(sorted(missing_placeholders))
            raise ValueError(f"Workflow still contains unresolved placeholders: {missing_list}")
        return rendered

    def workflow_requires_input_media(self, name: str) -> bool:
        workflow = self.load_workflow(name)
        placeholders = self._find_known_placeholders(workflow)
        if "{INPUT_IMAGE}" in placeholders:
            return True
        if self._looks_like_ui_workflow(workflow):
            return any(
                str(node.get("type", "")) in {"LoadImage", "LoadImageWithFilename"}
                for node in workflow.get("nodes", [])
            )
        return False

    def render_generation_workflow(
        self,
        name: str,
        prompt: str,
        seed: int,
        input_image: str | None = None,
        negative_prompt: str | None = None,
        length_frames: int | None = None,
        lora_overrides: dict[str, dict[str, Any]] | None = None,
        text_overrides: dict[str, str] | None = None,
        image_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        workflow = self.load_workflow(name)
        placeholders: dict[str, Any] = {
            "{PROMPT}": prompt,
            "{SEED}": seed,
        }
        if negative_prompt is not None:
            placeholders["{NEGATIVE_PROMPT}"] = negative_prompt
        if length_frames is not None:
            placeholders["{LENGTH}"] = length_frames
        if input_image is not None:
            placeholders["{INPUT_IMAGE}"] = input_image

        rendered = self._replace_placeholders(workflow, {key: str(value) for key, value in placeholders.items()})
        if self._looks_like_ui_workflow(rendered):
            self._inject_image_inputs_into_ui_workflow(rendered, input_image, image_overrides)
            if lora_overrides:
                self._inject_lora_overrides_into_ui_workflow(rendered, lora_overrides)
            self._inject_conditioning_prompts_into_ui_workflow(rendered, prompt, negative_prompt)
            if length_frames is not None:
                self._inject_length_into_ui_workflow(rendered, length_frames)
            if text_overrides:
                self._inject_text_overrides_into_ui_workflow(rendered, text_overrides)
        if self._looks_like_ui_workflow(rendered):
            rendered = self._convert_ui_workflow_to_prompt(rendered)
        missing = self._find_known_placeholders(rendered)
        if "{INPUT_IMAGE}" in missing and input_image is None:
            raise ValueError("Workflow requires an input image but none was provided.")
        if prompt:
            self._inject_prompt_into_workflow(rendered, prompt)
        if seed:
            self._inject_seed_into_workflow(rendered, seed)

        missing = self._find_known_placeholders(rendered)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise ValueError(f"Workflow still contains unresolved placeholders: {missing_list}")
        return rendered

    def inspect_power_lora_slots(self, name: str) -> list[dict[str, Any]]:
        workflow = self.load_workflow(name)
        if not self._looks_like_ui_workflow(workflow):
            return []
        slots: list[dict[str, Any]] = []
        slot_number = 1
        for node in workflow.get("nodes", []):
            if str(node.get("type", "")) != "Power Lora Loader (rgthree)":
                continue
            for widget_index, widget_value in enumerate(node.get("widgets_values", [])):
                if not self._is_power_lora_slot(widget_value):
                    continue
                slots.append(
                    {
                        "slot": slot_number,
                        "node_id": int(node["id"]),
                        "widget_index": widget_index,
                        "on": bool(widget_value.get("on", False)),
                        "lora": str(widget_value.get("lora") or ""),
                        "strength": self._coerce_float(widget_value.get("strength")),
                    }
                )
                slot_number += 1
        return slots

    def inspect_text_prompt_fields(self, name: str) -> list[dict[str, Any]]:
        workflow = self.load_workflow(name)
        if not self._looks_like_ui_workflow(workflow):
            return []
        fields: list[dict[str, Any]] = []
        for field in self._collect_text_prompt_targets(workflow):
            fields.append(
                {
                    "key": field["key"],
                    "label": field["label"],
                    "graph": field["graph"],
                    "node_id": int(field["node"].get("id", 0)),
                    "value": self._get_ui_text_node_value(field["node"]),
                }
            )
        return fields

    def inspect_image_input_fields(self, name: str) -> list[dict[str, Any]]:
        workflow = self.load_workflow(name)
        if not self._looks_like_ui_workflow(workflow):
            return []
        fields: list[dict[str, Any]] = []
        slug_counts: dict[str, int] = {}
        image_index = 0
        for node in workflow.get("nodes", []):
            if not self._is_image_input_node(node):
                continue
            image_index += 1
            label = str(node.get("title") or "").strip() or f"Image Input {image_index}"
            key = self._build_unique_text_prompt_key(label, slug_counts)
            fields.append(
                {
                    "key": key,
                    "label": label,
                    "graph": "main",
                    "node_id": int(node.get("id", 0)),
                    "value": self._get_ui_image_node_value(node),
                }
            )
        return fields

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

    def _find_unresolved_placeholders(self, value: Any, placeholder_keys: list[str]) -> set[str]:
        unresolved: set[str] = set()
        if isinstance(value, dict):
            for item in value.values():
                unresolved.update(self._find_unresolved_placeholders(item, placeholder_keys))
            return unresolved
        if isinstance(value, list):
            for item in value:
                unresolved.update(self._find_unresolved_placeholders(item, placeholder_keys))
            return unresolved
        if isinstance(value, str):
            for placeholder in placeholder_keys:
                if placeholder in value:
                    unresolved.add(placeholder)
        return unresolved

    def _find_known_placeholders(self, value: Any) -> set[str]:
        return self._find_unresolved_placeholders(value, list(self.KNOWN_PLACEHOLDERS))

    def _inject_prompt_into_workflow(self, workflow: dict[str, Any], prompt: str) -> None:
        if "nodes" not in workflow:
            for node in workflow.values():
                if not isinstance(node, dict):
                    continue
                if node.get("class_type") != "CLIPTextEncode":
                    continue
                inputs = node.setdefault("inputs", {})
                if isinstance(inputs.get("text"), list):
                    continue
                inputs["text"] = prompt
                break
            return

        for node in workflow.get("nodes", []):
            if node.get("type") != "CLIPTextEncode":
                continue
            inputs = node.get("inputs", [])
            text_input = next((item for item in inputs if item.get("name") == "text"), None)
            if text_input is None or text_input.get("link") is not None:
                continue
            widgets = node.setdefault("widgets_values", [])
            if widgets:
                widgets[0] = prompt
            else:
                node["widgets_values"] = [prompt]
            break

    def _inject_conditioning_prompts_into_ui_workflow(
        self,
        workflow: dict[str, Any],
        prompt: str,
        negative_prompt: str | None,
    ) -> None:
        link_map = {item[0]: item for item in workflow.get("links", [])}
        applied_positive = False
        applied_negative = False

        for node in workflow.get("nodes", []):
            if str(node.get("type", "")) != "LTXVConditioning":
                continue
            positive_source = self._get_upstream_node_for_input(workflow, link_map, node, "positive")
            negative_source = self._get_upstream_node_for_input(workflow, link_map, node, "negative")
            if positive_source is not None:
                self._set_ui_text_node_value(positive_source, prompt)
                applied_positive = True
            if negative_prompt is not None and negative_source is not None:
                self._set_ui_text_node_value(negative_source, negative_prompt)
                applied_negative = True

        clip_nodes = [node for node in workflow.get("nodes", []) if str(node.get("type", "")) == "CLIPTextEncode"]
        if not applied_positive and clip_nodes:
            self._set_ui_text_node_value(clip_nodes[0], prompt)
        if negative_prompt is not None and not applied_negative and len(clip_nodes) > 1:
            self._set_ui_text_node_value(clip_nodes[1], negative_prompt)

    def _inject_image_inputs_into_ui_workflow(
        self,
        workflow: dict[str, Any],
        input_image: str | None,
        image_overrides: dict[str, str] | None,
    ) -> None:
        fields = self._collect_image_input_targets(workflow)
        if not fields:
            return

        override_values = {str(key).strip().lower(): str(value) for key, value in (image_overrides or {}).items() if value}
        overridden_node_ids: set[int] = set()

        for field in fields:
            override_value = override_values.get(field["key"])
            if not override_value:
                continue
            self._set_ui_image_node_value(field["node"], override_value)
            overridden_node_ids.add(int(field["node"].get("id", 0)))

        if input_image is None:
            return

        unlabeled_fields = [
            field for field in fields
            if int(field["node"].get("id", 0)) not in overridden_node_ids and self._is_generic_image_input_label(field["label"])
        ]
        if len(unlabeled_fields) > 1:
            for field in unlabeled_fields:
                self._set_ui_image_node_value(field["node"], input_image)
            return

        target_field = self._pick_default_input_image_field(fields, overridden_node_ids)
        if target_field is None:
            return
        self._set_ui_image_node_value(target_field["node"], input_image)

    def _inject_length_into_ui_workflow(self, workflow: dict[str, Any], length_frames: int) -> None:
        for node in workflow.get("nodes", []):
            title = str(node.get("title", "")).strip().lower()
            if "length" not in title:
                continue
            widgets = node.setdefault("widgets_values", [])
            if widgets:
                widgets[0] = length_frames
            else:
                node["widgets_values"] = [length_frames]
            break

    def _inject_lora_overrides_into_ui_workflow(
        self,
        workflow: dict[str, Any],
        lora_overrides: dict[str, dict[str, Any]],
    ) -> None:
        slot_number = 1
        for node in workflow.get("nodes", []):
            if str(node.get("type", "")) != "Power Lora Loader (rgthree)":
                continue
            widgets = node.get("widgets_values", [])
            for widget_index, widget_value in enumerate(widgets):
                if not self._is_power_lora_slot(widget_value):
                    continue
                override = lora_overrides.get(str(slot_number))
                if override:
                    updated = dict(widget_value)
                    if "on" in override:
                        updated["on"] = bool(override["on"])
                    if "lora" in override:
                        updated["lora"] = str(override["lora"] or "")
                    if "strength" in override and override["strength"] is not None:
                        updated["strength"] = self._coerce_float(override["strength"])
                    widgets[widget_index] = updated
                slot_number += 1

    def _inject_text_overrides_into_ui_workflow(
        self,
        workflow: dict[str, Any],
        text_overrides: dict[str, str],
    ) -> None:
        for field in self._collect_text_prompt_targets(workflow):
            if field["key"] not in text_overrides:
                continue
            self._set_ui_text_node_value(field["node"], text_overrides[field["key"]])

    def _get_upstream_node_for_input(
        self,
        workflow: dict[str, Any],
        link_map: dict[int, list[Any]],
        node: dict[str, Any],
        input_name: str,
    ) -> dict[str, Any] | None:
        node_by_id = {int(item.get("id")): item for item in workflow.get("nodes", []) if "id" in item}
        for input_def in node.get("inputs", []):
            if str(input_def.get("name", "")) != input_name:
                continue
            link_id = input_def.get("link")
            if link_id is None:
                return None
            link = link_map.get(link_id)
            if link is None:
                return None
            return node_by_id.get(int(link[1]))
        return None

    def _set_ui_text_node_value(self, node: dict[str, Any], text: str) -> None:
        if str(node.get("type", "")) != "CLIPTextEncode":
            return
        widgets = node.setdefault("widgets_values", [])
        if widgets:
            widgets[0] = text
        else:
            node["widgets_values"] = [text]

    def _get_ui_text_node_value(self, node: dict[str, Any]) -> str:
        widgets = node.get("widgets_values", [])
        if not widgets:
            return ""
        value = widgets[0]
        return str(value) if value is not None else ""

    def _collect_text_prompt_targets(self, workflow: dict[str, Any]) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        slug_counts: dict[str, int] = {}
        prompt_index = 0

        for node in workflow.get("nodes", []):
            if not self._is_unlinked_clip_text_node(node):
                continue
            prompt_index += 1
            label = str(node.get("title") or "").strip() or f"Prompt {prompt_index}"
            key = self._build_unique_text_prompt_key(label, slug_counts)
            targets.append(
                {
                    "key": key,
                    "label": label,
                    "graph": "main",
                    "node": node,
                }
            )

        for subgraph in workflow.get("definitions", {}).get("subgraphs", []):
            if not isinstance(subgraph, dict):
                continue
            subgraph_name = str(subgraph.get("name") or "").strip()
            for node in subgraph.get("nodes", []):
                if not self._is_unlinked_clip_text_node(node):
                    continue
                label = subgraph_name or str(node.get("title") or "").strip() or f"Subgraph Prompt {prompt_index + 1}"
                key = self._build_unique_text_prompt_key(label, slug_counts)
                targets.append(
                    {
                        "key": key,
                        "label": label,
                        "graph": f"subgraph:{subgraph_name or 'unnamed'}",
                        "node": node,
                    }
                )
        return targets

    def _collect_image_input_targets(self, workflow: dict[str, Any]) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        slug_counts: dict[str, int] = {}
        image_index = 0
        for node in workflow.get("nodes", []):
            if not self._is_image_input_node(node):
                continue
            image_index += 1
            label = str(node.get("title") or "").strip() or f"Image Input {image_index}"
            key = self._build_unique_text_prompt_key(label, slug_counts)
            targets.append(
                {
                    "key": key,
                    "label": label,
                    "graph": "main",
                    "node": node,
                }
            )
        return targets

    def _is_image_input_node(self, node: dict[str, Any]) -> bool:
        return str(node.get("type", "")) in {"LoadImage", "LoadImageWithFilename"}

    def _get_ui_image_node_value(self, node: dict[str, Any]) -> str:
        widgets = node.get("widgets_values", [])
        if not widgets:
            return ""
        value = widgets[0]
        return str(value) if value is not None else ""

    def _set_ui_image_node_value(self, node: dict[str, Any], image_name: str) -> None:
        widgets = node.setdefault("widgets_values", [])
        if widgets:
            widgets[0] = image_name
        else:
            node["widgets_values"] = [image_name]

    def _pick_default_input_image_field(
        self,
        fields: list[dict[str, Any]],
        overridden_node_ids: set[int],
    ) -> dict[str, Any] | None:
        available_fields = [field for field in fields if int(field["node"].get("id", 0)) not in overridden_node_ids]
        if not available_fields:
            return None
        preferred_terms = (
            "reference photo",
            "referance photo",
            "source photo",
            "source image",
            "input image",
        )
        for field in available_fields:
            key = str(field["key"]).replace("_", " ").lower()
            label = str(field["label"]).lower()
            haystack = f"{key} {label}"
            if any(term in haystack for term in preferred_terms):
                return field
        if len(available_fields) == 1:
            return available_fields[0]
        return available_fields[-1]

    def _is_generic_image_input_label(self, label: str) -> bool:
        normalized = str(label).strip().lower()
        return bool(re.fullmatch(r"image input \d+", normalized))

    def _is_unlinked_clip_text_node(self, node: dict[str, Any]) -> bool:
        if str(node.get("type", "")) != "CLIPTextEncode":
            return False
        for input_def in node.get("inputs", []):
            if str(input_def.get("name", "")) != "text":
                continue
            return input_def.get("link") is None
        return False

    def _build_unique_text_prompt_key(self, label: str, slug_counts: dict[str, int]) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_") or "prompt"
        count = slug_counts.get(slug, 0) + 1
        slug_counts[slug] = count
        if count == 1:
            return slug
        return f"{slug}_{count}"

    def _is_power_lora_slot(self, widget_value: Any) -> bool:
        return isinstance(widget_value, dict) and {"on", "lora", "strength"}.issubset(widget_value.keys())

    def _coerce_float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _inject_seed_into_workflow(self, workflow: dict[str, Any], seed: int) -> None:
        if "nodes" not in workflow:
            for node in workflow.values():
                if not isinstance(node, dict):
                    continue
                if node.get("class_type") == "KSampler":
                    inputs = node.setdefault("inputs", {})
                    if not isinstance(inputs.get("seed"), list):
                        inputs["seed"] = seed
                if node.get("class_type") == "RandomNoise":
                    inputs = node.setdefault("inputs", {})
                    if not isinstance(inputs.get("noise_seed"), list):
                        inputs["noise_seed"] = seed
            return

        for node in workflow.get("nodes", []):
            if node.get("type") != "KSampler":
                continue
            widgets = node.setdefault("widgets_values", [])
            if widgets:
                widgets[0] = seed
            else:
                node["widgets_values"] = [seed]

    def _looks_like_ui_workflow(self, workflow: dict[str, Any]) -> bool:
        nodes = workflow.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            return False
        first_node = nodes[0]
        return (
            isinstance(first_node, dict)
            and "id" in first_node
            and "type" in first_node
            and "inputs" in first_node
        )

    def _convert_ui_workflow_to_prompt(self, workflow: dict[str, Any]) -> dict[str, Any]:
        workflow = self._expand_embedded_subgraphs(workflow)
        object_info = self._load_object_info()
        link_map = {item[0]: item for item in workflow.get("links", [])}
        prompt: dict[str, Any] = {}

        for node in workflow.get("nodes", []):
            node_type = str(node.get("type", ""))
            if node_type not in object_info:
                continue
            node_info = object_info[node_type]
            widget_values = list(node.get("widgets_values", []))
            widget_index = 0
            inputs: dict[str, Any] = {}

            for input_def in node.get("inputs", []):
                input_name = str(input_def.get("name", ""))
                link_id = input_def.get("link")
                has_widget = "widget" in input_def
                if link_id is not None:
                    link = link_map.get(link_id)
                    if link is not None:
                        inputs[input_name] = [str(link[1]), int(link[2])]
                    if has_widget and widget_index < len(widget_values):
                        widget_index += 1
                        if self._input_uses_control_after_generate(node_info, input_name):
                            widget_index += 1
                    continue

                if not has_widget or widget_index >= len(widget_values):
                    continue

                inputs[input_name] = copy.deepcopy(widget_values[widget_index])
                widget_index += 1
                if self._input_uses_control_after_generate(node_info, input_name):
                    widget_index += 1

            prompt[str(node["id"])] = {
                "class_type": node_type,
                "inputs": inputs,
            }

        return prompt

    def _expand_embedded_subgraphs(self, workflow: dict[str, Any]) -> dict[str, Any]:
        subgraphs = workflow.get("definitions", {}).get("subgraphs", [])
        subgraph_defs = {
            str(item.get("id")): item
            for item in subgraphs
            if isinstance(item, dict) and item.get("id")
        }
        if not subgraph_defs:
            return workflow

        expanded = copy.deepcopy(workflow)
        expanded["nodes"], expanded["links"] = self._expand_graph_nodes_and_links(
            expanded.get("nodes", []),
            expanded.get("links", []),
            subgraph_defs,
        )
        return expanded

    def _expand_graph_nodes_and_links(
        self,
        nodes: list[dict[str, Any]],
        links: list[Any],
        subgraph_defs: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[list[Any]]]:
        working_nodes = copy.deepcopy(nodes)
        working_links = self._normalize_links(links)
        next_node_id = self._next_graph_node_id(working_nodes, subgraph_defs)
        next_link_id = self._next_graph_link_id(working_links, subgraph_defs)

        while True:
            subgraph_node = next(
                (
                    node
                    for node in working_nodes
                    if isinstance(node, dict) and str(node.get("type", "")) in subgraph_defs
                ),
                None,
            )
            if subgraph_node is None:
                break
            working_nodes, working_links, next_node_id, next_link_id = self._expand_single_subgraph_node(
                working_nodes,
                working_links,
                subgraph_node,
                subgraph_defs[str(subgraph_node.get("type"))],
                subgraph_defs,
                next_node_id,
                next_link_id,
            )

        return working_nodes, self._denormalize_links(working_links)

    def _expand_single_subgraph_node(
        self,
        nodes: list[dict[str, Any]],
        links: list[dict[str, Any]],
        subgraph_node: dict[str, Any],
        subgraph_def: dict[str, Any],
        subgraph_defs: dict[str, dict[str, Any]],
        next_node_id: int,
        next_link_id: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
        instance_id = int(subgraph_node["id"])
        normalized_def_links = self._normalize_links(subgraph_def.get("links", []))
        internal_nodes, internal_links = self._expand_graph_nodes_and_links(
            subgraph_def.get("nodes", []),
            normalized_def_links,
            subgraph_defs,
        )
        internal_links = self._normalize_links(internal_links)

        remapped_nodes: list[dict[str, Any]] = []
        node_id_map: dict[int, int] = {}
        for internal_node in internal_nodes:
            original_id = int(internal_node["id"])
            node_id_map[original_id] = next_node_id
            copied_node = copy.deepcopy(internal_node)
            copied_node["id"] = next_node_id
            remapped_nodes.append(copied_node)
            next_node_id += 1

        internal_link_map: dict[int, dict[str, Any]] = {}
        input_bridge_links: dict[int, list[dict[str, Any]]] = {}
        bridge_link_by_id: dict[int, dict[str, Any]] = {}
        output_bridge_links: dict[int, list[dict[str, Any]]] = {}
        remapped_internal_links: list[dict[str, Any]] = []

        for internal_link in internal_links:
            link_id = int(internal_link["id"])
            origin_id = int(internal_link["origin_id"])
            target_id = int(internal_link["target_id"])
            if origin_id == -10:
                copied_bridge = copy.deepcopy(internal_link)
                input_bridge_links.setdefault(target_id, []).append(copied_bridge)
                bridge_link_by_id[link_id] = copied_bridge
                continue
            if target_id == -20:
                output_bridge_links.setdefault(origin_id, []).append(copy.deepcopy(internal_link))
                continue
            copied_link = copy.deepcopy(internal_link)
            copied_link["id"] = next_link_id
            copied_link["origin_id"] = node_id_map[origin_id]
            copied_link["target_id"] = node_id_map[target_id]
            internal_link_map[link_id] = copied_link
            remapped_internal_links.append(copied_link)
            next_link_id += 1

        input_slot_bindings = self._extract_subgraph_instance_input_bindings(subgraph_node, links)
        connector_by_link_id: dict[int, dict[str, Any]] = {}
        connector_by_origin_slot = {
            index: connector
            for index, connector in enumerate(subgraph_def.get("inputs", []))
        }
        for connector in subgraph_def.get("inputs", []):
            for connector_link_id in connector.get("linkIds", []) or []:
                connector_by_link_id[int(connector_link_id)] = connector

        added_input_links: list[dict[str, Any]] = []
        remapped_node_by_original_id = {
            int(original_node["id"]): copied_node
            for original_node, copied_node in zip(internal_nodes, remapped_nodes, strict=False)
        }
        for original_node in internal_nodes:
            copied_node = remapped_node_by_original_id[int(original_node["id"])]
            for input_def in copied_node.get("inputs", []):
                link_id = input_def.get("link")
                if link_id is None:
                    continue
                mapped_internal_link = internal_link_map.get(int(link_id))
                if mapped_internal_link is not None:
                    input_def["link"] = mapped_internal_link["id"]
                    continue
                connector = connector_by_link_id.get(int(link_id))
                if connector is None:
                    bridge_link = bridge_link_by_id.get(int(link_id))
                    if bridge_link is not None and int(bridge_link.get("origin_id", 0)) == -10:
                        connector = connector_by_origin_slot.get(int(bridge_link.get("origin_slot", 0)))
                if connector is None:
                    continue
                binding = input_slot_bindings.get(str(connector.get("name", "")))
                if binding is None:
                    input_def["link"] = None
                    continue
                if binding["kind"] == "link":
                    cloned_link = {
                        "id": next_link_id,
                        "origin_id": binding["origin_id"],
                        "origin_slot": binding["origin_slot"],
                        "target_id": copied_node["id"],
                        "target_slot": int(input_def.get("slot_index", self._find_input_slot_index(copied_node, input_def))),
                        "type": binding["type"],
                    }
                    input_def["link"] = cloned_link["id"]
                    added_input_links.append(cloned_link)
                    next_link_id += 1
                    continue
                self._set_ui_widget_input_value(copied_node, str(input_def.get("name", "")), binding["value"])
                input_def["link"] = None

        output_slot_bindings: dict[int, tuple[int, int]] = {}
        output_connector_by_link_id: dict[int, tuple[int, dict[str, Any]]] = {}
        for output_index, connector in enumerate(subgraph_def.get("outputs", [])):
            for connector_link_id in connector.get("linkIds", []) or []:
                output_connector_by_link_id[int(connector_link_id)] = (output_index, connector)

        for original_origin_id, bridge_links in output_bridge_links.items():
            for bridge_link in bridge_links:
                output_binding = output_connector_by_link_id.get(int(bridge_link["id"]))
                if output_binding is None:
                    continue
                output_index, _connector = output_binding
                output_slot_bindings[output_index] = (
                    node_id_map[int(original_origin_id)],
                    int(bridge_link["origin_slot"]),
                )

        new_links: list[dict[str, Any]] = []
        for link in links:
            origin_id = int(link["origin_id"])
            target_id = int(link["target_id"])
            if target_id == instance_id:
                continue
            if origin_id != instance_id:
                new_links.append(link)
                continue
            output_binding = output_slot_bindings.get(int(link["origin_slot"]))
            if output_binding is None:
                continue
            rebound_link = copy.deepcopy(link)
            rebound_link["origin_id"] = output_binding[0]
            rebound_link["origin_slot"] = output_binding[1]
            new_links.append(rebound_link)

        new_nodes = [node for node in nodes if int(node["id"]) != instance_id]
        new_nodes.extend(remapped_nodes)
        new_links.extend(remapped_internal_links)
        new_links.extend(added_input_links)
        return new_nodes, new_links, next_node_id, next_link_id

    def _extract_subgraph_instance_input_bindings(
        self,
        node: dict[str, Any],
        links: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        link_by_id = {int(link["id"]): link for link in links}
        bindings: dict[str, dict[str, Any]] = {}
        widget_values = list(node.get("widgets_values", []))
        widget_index = 0

        for input_def in node.get("inputs", []):
            input_name = str(input_def.get("name", ""))
            link_id = input_def.get("link")
            has_widget = "widget" in input_def
            if link_id is not None:
                link = link_by_id.get(int(link_id))
                if link is not None:
                    bindings[input_name] = {
                        "kind": "link",
                        "origin_id": int(link["origin_id"]),
                        "origin_slot": int(link["origin_slot"]),
                        "type": str(link.get("type", "")),
                    }
                if has_widget and widget_index < len(widget_values):
                    widget_index += 1
                continue
            if not has_widget or widget_index >= len(widget_values):
                continue
            bindings[input_name] = {
                "kind": "value",
                "value": copy.deepcopy(widget_values[widget_index]),
            }
            widget_index += 1
        return bindings

    def _set_ui_widget_input_value(self, node: dict[str, Any], input_name: str, value: Any) -> None:
        inputs = node.get("inputs", [])
        target_index = 0
        found = False
        for input_def in inputs:
            if "widget" not in input_def:
                continue
            if str(input_def.get("name", "")) == input_name:
                found = True
                break
            target_index += 1
        if not found:
            return
        widgets = node.setdefault("widgets_values", [])
        while len(widgets) <= target_index:
            widgets.append(None)
        widgets[target_index] = copy.deepcopy(value)

    def _find_input_slot_index(self, node: dict[str, Any], target_input: dict[str, Any]) -> int:
        for index, input_def in enumerate(node.get("inputs", [])):
            if input_def is target_input:
                return index
        return 0

    def _normalize_links(self, links: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for link in links:
            if isinstance(link, dict):
                normalized.append(copy.deepcopy(link))
                continue
            if isinstance(link, list) and len(link) >= 6:
                normalized.append(
                    {
                        "id": int(link[0]),
                        "origin_id": int(link[1]),
                        "origin_slot": int(link[2]),
                        "target_id": int(link[3]),
                        "target_slot": int(link[4]),
                        "type": link[5],
                    }
                )
        return normalized

    def _denormalize_links(self, links: list[dict[str, Any]]) -> list[list[Any]]:
        return [
            [
                int(link["id"]),
                int(link["origin_id"]),
                int(link["origin_slot"]),
                int(link["target_id"]),
                int(link["target_slot"]),
                link.get("type"),
            ]
            for link in links
        ]

    def _next_graph_node_id(self, nodes: list[dict[str, Any]], subgraph_defs: dict[str, dict[str, Any]]) -> int:
        max_node_id = 0
        for node in nodes:
            if isinstance(node, dict) and "id" in node:
                max_node_id = max(max_node_id, int(node["id"]))
        for subgraph_def in subgraph_defs.values():
            for node in subgraph_def.get("nodes", []):
                if isinstance(node, dict) and "id" in node:
                    max_node_id = max(max_node_id, int(node["id"]))
        return max_node_id + 1

    def _next_graph_link_id(self, links: list[dict[str, Any]], subgraph_defs: dict[str, dict[str, Any]]) -> int:
        max_link_id = 0
        for link in links:
            max_link_id = max(max_link_id, int(link["id"]))
        for subgraph_def in subgraph_defs.values():
            for link in self._normalize_links(subgraph_def.get("links", [])):
                max_link_id = max(max_link_id, int(link["id"]))
        return max_link_id + 1

    def _input_uses_control_after_generate(self, node_info: dict[str, Any], input_name: str) -> bool:
        input_config = (
            node_info.get("input", {}).get("required", {}).get(input_name)
            or node_info.get("input", {}).get("optional", {}).get(input_name)
        )
        if not isinstance(input_config, list) or len(input_config) < 2:
            return False
        metadata = input_config[1]
        return isinstance(metadata, dict) and bool(metadata.get("control_after_generate"))

    def _load_object_info(self) -> dict[str, Any]:
        if self._object_info_cache is not None:
            return self._object_info_cache
        if self.object_info_loader is not None:
            self._object_info_cache = self.object_info_loader()
            return self._object_info_cache

        response = requests.get(f"{settings.COMFYUI_BASE_URL.rstrip('/')}/object_info", timeout=30)
        response.raise_for_status()
        self._object_info_cache = response.json()
        return self._object_info_cache
