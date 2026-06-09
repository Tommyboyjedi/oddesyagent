import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from apps.core.services.workflow_manager import WorkflowManager


class WorkflowManagerTests(SimpleTestCase):
    def test_list_workflows(self) -> None:
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "a.json").write_text("{}", encoding="utf-8")
            Path(temp_dir, "b.json").write_text("{}", encoding="utf-8")

            manager = WorkflowManager(workflow_dir=temp_dir)

            self.assertEqual(manager.list_workflows(), ["a", "b"])

    def test_path_traversal_is_blocked(self) -> None:
        with TemporaryDirectory() as temp_dir:
            manager = WorkflowManager(workflow_dir=temp_dir)
            with self.assertRaises(ValueError):
                manager.load_workflow("../secret")

    def test_nested_placeholders_are_replaced(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "sample.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "1": {
                            "inputs": {
                                "image": "{INPUT_IMAGE}",
                                "config": {
                                    "prompt": "{PROMPT}",
                                    "seed": "{SEED}",
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manager = WorkflowManager(workflow_dir=temp_dir)

            rendered = manager.render_workflow(
                "sample",
                {
                    "{INPUT_IMAGE}": "input.png",
                    "{PROMPT}": "make video",
                    "{SEED}": 42,
                },
            )

            self.assertEqual(rendered["1"]["inputs"]["image"], "input.png")
            self.assertEqual(rendered["1"]["inputs"]["config"]["prompt"], "make video")
            self.assertEqual(rendered["1"]["inputs"]["config"]["seed"], 42)

    def test_unresolved_placeholders_raise_value_error(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "sample.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "1": {
                            "inputs": {
                                "image": "{INPUT_IMAGE}",
                                "prompt": "{PROMPT}",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manager = WorkflowManager(workflow_dir=temp_dir)

            with self.assertRaises(ValueError):
                manager.render_workflow(
                    "sample",
                    {
                        "{INPUT_IMAGE}": "input.png",
                        "{PROMPT}": "{PROMPT}",
                    },
                )
