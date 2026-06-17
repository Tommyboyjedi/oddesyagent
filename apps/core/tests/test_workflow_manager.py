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

    def test_render_generation_workflow_injects_prompt_and_seed_into_prompt_only_workflow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "prompt_only.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "type": "CLIPTextEncode",
                                "inputs": [{"name": "text", "link": None}],
                                "widgets_values": ["old prompt"],
                            },
                            {
                                "type": "KSampler",
                                "widgets_values": [12345, "randomize", 30, 4],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manager = WorkflowManager(workflow_dir=temp_dir)

            rendered = manager.render_generation_workflow(
                "prompt_only",
                prompt="new prompt",
                seed=42,
            )

            self.assertEqual(rendered["nodes"][0]["widgets_values"][0], "new prompt")
            self.assertEqual(rendered["nodes"][1]["widgets_values"][0], 42)

    def test_workflow_requires_input_media_detects_input_placeholder(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "needs_image.json")
            workflow_path.write_text(json.dumps({"1": {"inputs": {"image": "{INPUT_IMAGE}"}}}), encoding="utf-8")
            manager = WorkflowManager(workflow_dir=temp_dir)

            self.assertTrue(manager.workflow_requires_input_media("needs_image"))

    def test_workflow_requires_input_media_detects_load_image_node(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "needs_image_ui.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 149,
                                "type": "LoadImage",
                                "inputs": [
                                    {"name": "image", "link": None, "widget": {"name": "image"}},
                                    {"name": "upload", "link": None, "widget": {"name": "upload"}},
                                ],
                                "widgets_values": ["headshot.png", "image"],
                            }
                        ],
                        "links": [],
                    }
                ),
                encoding="utf-8",
            )
            manager = WorkflowManager(workflow_dir=temp_dir)

            self.assertTrue(manager.workflow_requires_input_media("needs_image_ui"))

    def test_workflow_requires_input_media_detects_load_image_with_filename_node(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "needs_image_ui_filename.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 17,
                                "type": "LoadImageWithFilename",
                                "inputs": [
                                    {"name": "image", "link": None, "widget": {"name": "image"}},
                                    {"name": "upload", "link": None, "widget": {"name": "upload"}},
                                ],
                                "widgets_values": ["reference.png", "image"],
                            }
                        ],
                        "links": [],
                    }
                ),
                encoding="utf-8",
            )
            manager = WorkflowManager(workflow_dir=temp_dir)

            self.assertTrue(manager.workflow_requires_input_media("needs_image_ui_filename"))

    def test_inspect_image_input_fields_lists_main_graph_image_nodes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "image_fields.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 17,
                                "type": "LoadImageWithFilename",
                                "title": "Face or Outfit Referance",
                                "inputs": [
                                    {"name": "image", "link": None, "widget": {"name": "image"}},
                                    {"name": "upload", "link": None, "widget": {"name": "upload"}},
                                ],
                                "widgets_values": ["face_ref.png", "image"],
                            },
                            {
                                "id": 219,
                                "type": "LoadImage",
                                "title": "Referance Photo",
                                "inputs": [
                                    {"name": "image", "link": None, "widget": {"name": "image"}},
                                    {"name": "upload", "link": None, "widget": {"name": "upload"}},
                                ],
                                "widgets_values": ["source.png", "image"],
                            },
                        ],
                        "links": [],
                    }
                ),
                encoding="utf-8",
            )
            manager = WorkflowManager(workflow_dir=temp_dir)

            fields = manager.inspect_image_input_fields("image_fields")

            self.assertEqual(fields[0]["key"], "face_or_outfit_referance")
            self.assertEqual(fields[0]["value"], "face_ref.png")
            self.assertEqual(fields[1]["key"], "referance_photo")
            self.assertEqual(fields[1]["value"], "source.png")

    def test_render_generation_workflow_converts_ui_workflow_to_api_prompt(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "ui_prompt.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 4,
                                "type": "CLIPTextEncode",
                                "inputs": [
                                    {"name": "clip", "link": 39},
                                    {"name": "text", "link": None, "widget": {"name": "text"}},
                                ],
                                "widgets_values": ["old prompt"],
                            },
                            {
                                "id": 7,
                                "type": "KSampler",
                                "inputs": [
                                    {"name": "model", "link": 41},
                                    {"name": "positive", "link": 42},
                                    {"name": "negative", "link": 89},
                                    {"name": "latent_image", "link": 44},
                                    {"name": "seed", "link": None, "widget": {"name": "seed"}},
                                    {"name": "steps", "link": None, "widget": {"name": "steps"}},
                                    {"name": "cfg", "link": None, "widget": {"name": "cfg"}},
                                    {"name": "sampler_name", "link": None, "widget": {"name": "sampler_name"}},
                                    {"name": "scheduler", "link": None, "widget": {"name": "scheduler"}},
                                    {"name": "denoise", "link": None, "widget": {"name": "denoise"}},
                                ],
                                "widgets_values": [713978875376862, "randomize", 30, 4, "dpmpp_3m_sde_gpu", "karras", 1],
                            },
                        ],
                        "links": [
                            [39, 3, 0, 4, 0, "CLIP"],
                            [41, 1, 0, 7, 0, "MODEL"],
                            [42, 4, 0, 7, 1, "CONDITIONING"],
                            [89, 64, 0, 7, 2, "CONDITIONING"],
                            [44, 6, 0, 7, 3, "LATENT"],
                        ],
                    }
                ),
                encoding="utf-8",
            )
            object_info = {
                "CLIPTextEncode": {
                    "input": {"required": {"text": ["STRING", {}], "clip": ["CLIP", {}]}},
                },
                "KSampler": {
                    "input": {
                        "required": {
                            "model": ["MODEL", {}],
                            "seed": ["INT", {"control_after_generate": True}],
                            "steps": ["INT", {}],
                            "cfg": ["FLOAT", {}],
                            "sampler_name": [["dpmpp_3m_sde_gpu"], {}],
                            "scheduler": [["karras"], {}],
                            "positive": ["CONDITIONING", {}],
                            "negative": ["CONDITIONING", {}],
                            "latent_image": ["LATENT", {}],
                            "denoise": ["FLOAT", {}],
                        }
                    },
                },
            }
            manager = WorkflowManager(workflow_dir=temp_dir, object_info_loader=lambda: object_info)

            rendered = manager.render_generation_workflow(
                "ui_prompt",
                prompt="new prompt",
                seed=42,
            )

            self.assertEqual(rendered["4"]["class_type"], "CLIPTextEncode")
            self.assertEqual(rendered["4"]["inputs"]["text"], "new prompt")
            self.assertEqual(rendered["4"]["inputs"]["clip"], ["3", 0])
            self.assertEqual(rendered["7"]["inputs"]["seed"], 42)
            self.assertEqual(rendered["7"]["inputs"]["steps"], 30)
            self.assertEqual(rendered["7"]["inputs"]["cfg"], 4)
            self.assertEqual(rendered["7"]["inputs"]["sampler_name"], "dpmpp_3m_sde_gpu")
            self.assertEqual(rendered["7"]["inputs"]["scheduler"], "karras")
            self.assertEqual(rendered["7"]["inputs"]["denoise"], 1)

    def test_render_generation_workflow_injects_input_image_into_load_image_with_filename(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "ui_prompt_image_filename.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 17,
                                "type": "LoadImageWithFilename",
                                "inputs": [
                                    {"name": "image", "link": None, "widget": {"name": "image"}},
                                    {"name": "upload", "link": None, "widget": {"name": "upload"}},
                                ],
                                "widgets_values": ["reference.png", "image"],
                            }
                        ],
                        "links": [],
                    }
                ),
                encoding="utf-8",
            )
            object_info = {
                "LoadImageWithFilename": {
                    "input": {"required": {"image": [["reference.png"], {}]}},
                }
            }
            manager = WorkflowManager(workflow_dir=temp_dir, object_info_loader=lambda: object_info)

            rendered = manager.render_generation_workflow(
                "ui_prompt_image_filename",
                prompt="",
                seed=0,
                input_image="uploaded.png",
            )

            self.assertEqual(rendered["17"]["class_type"], "LoadImageWithFilename")
            self.assertEqual(rendered["17"]["inputs"]["image"], "uploaded.png")
            self.assertEqual(rendered["17"]["inputs"]["upload"], "image")

    def test_render_generation_workflow_routes_default_and_override_images_to_separate_fields(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "ui_multiple_images.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 17,
                                "type": "LoadImageWithFilename",
                                "title": "Face or Outfit Referance",
                                "inputs": [
                                    {"name": "image", "link": None, "widget": {"name": "image"}},
                                    {"name": "upload", "link": None, "widget": {"name": "upload"}},
                                ],
                                "widgets_values": ["face_ref.png", "image"],
                            },
                            {
                                "id": 219,
                                "type": "LoadImage",
                                "title": "Referance Photo",
                                "inputs": [
                                    {"name": "image", "link": None, "widget": {"name": "image"}},
                                    {"name": "upload", "link": None, "widget": {"name": "upload"}},
                                ],
                                "widgets_values": ["source.png", "image"],
                            },
                        ],
                        "links": [],
                    }
                ),
                encoding="utf-8",
            )
            object_info = {
                "LoadImageWithFilename": {
                    "input": {"required": {"image": [["face_ref.png"], {}]}},
                },
                "LoadImage": {
                    "input": {"required": {"image": [["source.png"], {}]}},
                },
            }
            manager = WorkflowManager(workflow_dir=temp_dir, object_info_loader=lambda: object_info)

            rendered = manager.render_generation_workflow(
                "ui_multiple_images",
                prompt="",
                seed=0,
                input_image="uploaded_source.png",
                image_overrides={"face_or_outfit_referance": "steer_face.png"},
            )

            self.assertEqual(rendered["17"]["inputs"]["image"], "steer_face.png")
            self.assertEqual(rendered["219"]["inputs"]["image"], "uploaded_source.png")

    def test_render_generation_workflow_injects_input_image_into_all_generic_image_slots(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "ui_generic_multiple_images.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 149,
                                "type": "LoadImage",
                                "inputs": [
                                    {"name": "image", "link": None, "widget": {"name": "image"}},
                                    {"name": "upload", "link": None, "widget": {"name": "upload"}},
                                ],
                                "widgets_values": ["saved_a.png", "image"],
                            },
                            {
                                "id": 207,
                                "type": "LoadImage",
                                "inputs": [
                                    {"name": "image", "link": None, "widget": {"name": "image"}},
                                    {"name": "upload", "link": None, "widget": {"name": "upload"}},
                                ],
                                "widgets_values": ["saved_b.png", "image"],
                            },
                        ],
                        "links": [],
                    }
                ),
                encoding="utf-8",
            )
            object_info = {
                "LoadImage": {
                    "input": {"required": {"image": [["saved_a.png", "saved_b.png"], {}]}},
                },
            }
            manager = WorkflowManager(workflow_dir=temp_dir, object_info_loader=lambda: object_info)

            rendered = manager.render_generation_workflow(
                "ui_generic_multiple_images",
                prompt="",
                seed=0,
                input_image="uploaded_source.png",
            )

            self.assertEqual(rendered["149"]["inputs"]["image"], "uploaded_source.png")
            self.assertEqual(rendered["207"]["inputs"]["image"], "uploaded_source.png")

    def test_render_generation_workflow_skips_ui_only_nodes_missing_from_object_info(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "ui_prompt_with_note.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 131,
                                "type": "MarkdownNote",
                                "inputs": [],
                                "widgets_values": ["note text"],
                            },
                            {
                                "id": 4,
                                "type": "CLIPTextEncode",
                                "inputs": [
                                    {"name": "clip", "link": 39},
                                    {"name": "text", "link": None, "widget": {"name": "text"}},
                                ],
                                "widgets_values": ["old prompt"],
                            },
                        ],
                        "links": [
                            [39, 3, 0, 4, 0, "CLIP"],
                        ],
                    }
                ),
                encoding="utf-8",
            )
            object_info = {
                "CLIPTextEncode": {
                    "input": {"required": {"text": ["STRING", {}], "clip": ["CLIP", {}]}},
                },
            }
            manager = WorkflowManager(workflow_dir=temp_dir, object_info_loader=lambda: object_info)

            rendered = manager.render_generation_workflow(
                "ui_prompt_with_note",
                prompt="new prompt",
                seed=0,
            )

            self.assertNotIn("131", rendered)
            self.assertIn("4", rendered)
            self.assertEqual(rendered["4"]["inputs"]["text"], "new prompt")

    def test_render_generation_workflow_expands_embedded_subgraphs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "ui_embedded_subgraph.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 1,
                                "type": "LoadImage",
                                "inputs": [
                                    {"name": "image", "link": None, "widget": {"name": "image"}},
                                    {"name": "upload", "link": None, "widget": {"name": "upload"}},
                                ],
                                "widgets_values": ["headshot.png", "image"],
                            },
                            {
                                "id": 2,
                                "type": "outer-subgraph",
                                "inputs": [
                                    {"name": "image", "link": 11},
                                ],
                                "widgets_values": [],
                            },
                            {
                                "id": 3,
                                "type": "MaskBlur",
                                "inputs": [
                                    {"name": "mask", "link": 12},
                                    {"name": "amount", "link": None, "widget": {"name": "amount"}},
                                ],
                                "widgets_values": [4],
                            },
                        ],
                        "links": [
                            [11, 1, 0, 2, 0, "IMAGE"],
                            [12, 2, 0, 3, 0, "MASK"],
                        ],
                        "definitions": {
                            "subgraphs": [
                                {
                                    "id": "outer-subgraph",
                                    "name": "Outer",
                                    "inputs": [
                                        {"name": "image", "type": "IMAGE", "linkIds": [101]},
                                    ],
                                    "outputs": [
                                        {"name": "mask", "type": "MASK", "linkIds": [102]},
                                    ],
                                    "nodes": [
                                        {
                                            "id": 10,
                                            "type": "inner-subgraph",
                                            "inputs": [
                                                {"name": "image", "link": 101},
                                            ],
                                            "widgets_values": [],
                                        }
                                    ],
                                    "links": [
                                        {"id": 101, "origin_id": -10, "origin_slot": 0, "target_id": 10, "target_slot": 0, "type": "IMAGE"},
                                        {"id": 102, "origin_id": 10, "origin_slot": 0, "target_id": -20, "target_slot": 0, "type": "MASK"},
                                    ],
                                },
                                {
                                    "id": "inner-subgraph",
                                    "name": "Inner",
                                    "inputs": [
                                        {"name": "image", "type": "IMAGE", "linkIds": [201]},
                                    ],
                                    "outputs": [
                                        {"name": "mask", "type": "MASK", "linkIds": [202]},
                                    ],
                                    "nodes": [
                                        {
                                            "id": 20,
                                            "type": "ImageToMask",
                                            "inputs": [
                                                {"name": "image", "link": 201},
                                            ],
                                            "widgets_values": [],
                                        }
                                    ],
                                    "links": [
                                        {"id": 201, "origin_id": -10, "origin_slot": 0, "target_id": 20, "target_slot": 0, "type": "IMAGE"},
                                        {"id": 202, "origin_id": 20, "origin_slot": 0, "target_id": -20, "target_slot": 0, "type": "MASK"},
                                    ],
                                },
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            object_info = {
                "LoadImage": {
                    "input": {"required": {"image": [["headshot.png"], {}]}},
                },
                "ImageToMask": {
                    "input": {"required": {"image": ["IMAGE", {}]}},
                },
                "MaskBlur": {
                    "input": {"required": {"mask": ["MASK", {}], "amount": ["INT", {}]}},
                },
            }
            manager = WorkflowManager(workflow_dir=temp_dir, object_info_loader=lambda: object_info)

            rendered = manager.render_generation_workflow(
                "ui_embedded_subgraph",
                prompt="",
                seed=0,
                input_image="example.png",
            )

            self.assertIn("1", rendered)
            self.assertIn("3", rendered)
            self.assertIn("21", rendered)
            self.assertEqual(rendered["1"]["inputs"]["image"], "example.png")
            self.assertEqual(rendered["21"]["class_type"], "ImageToMask")
            self.assertEqual(rendered["21"]["inputs"]["image"], ["1", 0])
            self.assertEqual(rendered["3"]["inputs"]["mask"], ["21", 0])
            self.assertEqual(rendered["3"]["inputs"]["amount"], 4)

    def test_render_generation_workflow_preserves_widget_alignment_when_linked_inputs_have_widgets(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "ui_linked_widget_alignment.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 105,
                                "type": "GetImageSize",
                                "inputs": [
                                    {"name": "image", "link": 277},
                                ],
                                "widgets_values": [],
                            },
                            {
                                "id": 108,
                                "type": "EmptyLTXVLatentVideo",
                                "inputs": [
                                    {"name": "width", "link": 287, "widget": {"name": "width"}},
                                    {"name": "height", "link": 288, "widget": {"name": "height"}},
                                    {"name": "length", "link": 289, "widget": {"name": "length"}},
                                    {"name": "batch_size", "link": None, "widget": {"name": "batch_size"}},
                                ],
                                "widgets_values": [768, 512, 97, 1],
                            },
                            {
                                "id": 171,
                                "type": "LTXVEmptyLatentAudio",
                                "inputs": [
                                    {"name": "audio_vae", "link": 445},
                                    {"name": "frames_number", "link": 381, "widget": {"name": "frames_number"}},
                                    {"name": "frame_rate", "link": 382, "widget": {"name": "frame_rate"}},
                                    {"name": "batch_size", "link": None, "widget": {"name": "batch_size"}},
                                ],
                                "widgets_values": [97, 25, 1],
                            },
                        ],
                        "links": [
                            [277, 104, 0, 105, 0, "IMAGE"],
                            [287, 105, 0, 108, 0, "INT"],
                            [288, 105, 1, 108, 1, "INT"],
                            [289, 112, 0, 108, 2, "INT"],
                            [381, 112, 0, 171, 1, "INT"],
                            [382, 130, 0, 171, 2, "INT"],
                            [445, 202, 0, 171, 0, "VAE"],
                        ],
                    }
                ),
                encoding="utf-8",
            )
            object_info = {
                "GetImageSize": {
                    "input": {"required": {"image": ["IMAGE", {}]}},
                },
                "EmptyLTXVLatentVideo": {
                    "input": {
                        "required": {
                            "width": ["INT", {}],
                            "height": ["INT", {}],
                            "length": ["INT", {}],
                            "batch_size": ["INT", {}],
                        }
                    },
                },
                "LTXVEmptyLatentAudio": {
                    "input": {
                        "required": {
                            "audio_vae": ["VAE", {}],
                            "frames_number": ["INT", {}],
                            "frame_rate": ["INT", {}],
                            "batch_size": ["INT", {}],
                        }
                    },
                },
            }
            manager = WorkflowManager(workflow_dir=temp_dir, object_info_loader=lambda: object_info)

            rendered = manager.render_generation_workflow(
                "ui_linked_widget_alignment",
                prompt="",
                seed=0,
            )

            self.assertEqual(rendered["108"]["inputs"]["width"], ["105", 0])
            self.assertEqual(rendered["108"]["inputs"]["height"], ["105", 1])
            self.assertEqual(rendered["108"]["inputs"]["length"], ["112", 0])
            self.assertEqual(rendered["108"]["inputs"]["batch_size"], 1)
            self.assertEqual(rendered["171"]["inputs"]["frames_number"], ["112", 0])
            self.assertEqual(rendered["171"]["inputs"]["frame_rate"], ["130", 0])
            self.assertEqual(rendered["171"]["inputs"]["batch_size"], 1)

    def test_render_generation_workflow_injects_video_fields_into_ui_workflow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "ui_video.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 121,
                                "type": "CLIPTextEncode",
                                "inputs": [
                                    {"name": "clip", "link": 449},
                                    {"name": "text", "link": None, "widget": {"name": "text"}},
                                ],
                                "widgets_values": ["old positive"],
                            },
                            {
                                "id": 110,
                                "type": "CLIPTextEncode",
                                "inputs": [
                                    {"name": "clip", "link": 450},
                                    {"name": "text", "link": None, "widget": {"name": "text"}},
                                ],
                                "widgets_values": ["old negative"],
                            },
                            {
                                "id": 107,
                                "type": "LTXVConditioning",
                                "inputs": [
                                    {"name": "positive", "link": 283},
                                    {"name": "negative", "link": 284},
                                    {"name": "frame_rate", "link": None, "widget": {"name": "frame_rate"}},
                                ],
                                "widgets_values": [25],
                            },
                            {
                                "id": 149,
                                "type": "LoadImage",
                                "inputs": [
                                    {"name": "image", "link": None, "widget": {"name": "image"}},
                                    {"name": "upload", "link": None, "widget": {"name": "upload"}},
                                ],
                                "widgets_values": ["headshot.png", "image"],
                            },
                            {
                                "id": 112,
                                "type": "PrimitiveInt",
                                "title": "Length",
                                "inputs": [{"name": "value", "link": None, "widget": {"name": "value"}}],
                                "widgets_values": [500, "fixed"],
                            },
                        ],
                        "links": [
                            [283, 121, 0, 107, 0, "CONDITIONING"],
                            [284, 110, 0, 107, 1, "CONDITIONING"],
                            [449, 203, 1, 121, 0, "CLIP"],
                            [450, 203, 1, 110, 0, "CLIP"],
                        ],
                    }
                ),
                encoding="utf-8",
            )
            object_info = {
                "CLIPTextEncode": {
                    "input": {"required": {"text": ["STRING", {}], "clip": ["CLIP", {}]}},
                },
                "LTXVConditioning": {
                    "input": {
                        "required": {
                            "positive": ["CONDITIONING", {}],
                            "negative": ["CONDITIONING", {}],
                            "frame_rate": ["FLOAT", {}],
                        }
                    },
                },
                "LoadImage": {
                    "input": {"required": {"image": ["STRING", {}], "upload": ["IMAGEUPLOAD", {}]}},
                },
                "PrimitiveInt": {
                    "input": {"required": {"value": ["INT", {}]}},
                },
            }
            manager = WorkflowManager(workflow_dir=temp_dir, object_info_loader=lambda: object_info)

            rendered = manager.render_generation_workflow(
                "ui_video",
                prompt="new positive",
                negative_prompt="new negative",
                seed=42,
                input_image="latest.png",
                length_frames=180,
            )

            self.assertEqual(rendered["121"]["inputs"]["text"], "new positive")
            self.assertEqual(rendered["110"]["inputs"]["text"], "new negative")
            self.assertEqual(rendered["149"]["inputs"]["image"], "latest.png")
            self.assertEqual(rendered["112"]["inputs"]["value"], 180)

    def test_inspect_power_lora_slots_returns_slots(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "ui_loras.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 59,
                                "type": "Power Lora Loader (rgthree)",
                                "inputs": [],
                                "widgets_values": [
                                    {},
                                    {"type": "PowerLoraLoaderHeaderWidget"},
                                    {"on": True, "lora": "pony\\a.safetensors", "strength": 1.0, "strengthTwo": None},
                                    {"on": False, "lora": "pony\\b.safetensors", "strength": 0.5, "strengthTwo": None},
                                    {},
                                    "",
                                ],
                            }
                        ],
                        "links": [],
                    }
                ),
                encoding="utf-8",
            )
            manager = WorkflowManager(workflow_dir=temp_dir)

            slots = manager.inspect_power_lora_slots("ui_loras")

            self.assertEqual(len(slots), 2)
            self.assertEqual(slots[0]["slot"], 1)
            self.assertEqual(slots[0]["lora"], "pony\\a.safetensors")
            self.assertTrue(slots[0]["on"])
            self.assertEqual(slots[1]["slot"], 2)
            self.assertFalse(slots[1]["on"])

    def test_render_generation_workflow_applies_power_lora_overrides(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "ui_loras.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 59,
                                "type": "Power Lora Loader (rgthree)",
                                "inputs": [],
                                "widgets_values": [
                                    {},
                                    {"type": "PowerLoraLoaderHeaderWidget"},
                                    {"on": True, "lora": "pony\\a.safetensors", "strength": 1.0, "strengthTwo": None},
                                ],
                            }
                        ],
                        "links": [],
                    }
                ),
                encoding="utf-8",
            )
            manager = WorkflowManager(workflow_dir=temp_dir)
            workflow = manager.load_workflow("ui_loras")

            manager._inject_lora_overrides_into_ui_workflow(  # noqa: SLF001
                workflow,
                {"1": {"on": False, "lora": "pony\\override.safetensors", "strength": 0.7}},
            )

            slot = workflow["nodes"][0]["widgets_values"][2]
            self.assertFalse(slot["on"])
            self.assertEqual(slot["lora"], "pony\\override.safetensors")
            self.assertEqual(slot["strength"], 0.7)

    def test_inspect_text_prompt_fields_includes_main_graph_and_subgraphs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "ui_subgraph_prompts.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 1,
                                "type": "CLIPTextEncode",
                                "title": "Positive Prompt",
                                "inputs": [
                                    {"name": "clip", "link": 10},
                                    {"name": "text", "link": None, "widget": {"name": "text"}},
                                ],
                                "widgets_values": ["old positive"],
                            }
                        ],
                        "links": [[10, 2, 0, 1, 0, "CLIP"]],
                        "definitions": {
                            "subgraphs": [
                                {
                                    "name": "Sam3.1 Prompt Section A",
                                    "nodes": [
                                        {
                                            "id": 264,
                                            "type": "CLIPTextEncode",
                                            "inputs": [
                                                {"name": "clip", "link": 851},
                                                {"name": "text", "link": None, "widget": {"name": "text"}},
                                            ],
                                            "widgets_values": ["face"],
                                        }
                                    ],
                                },
                                {
                                    "name": "Face or Outfit Prompt",
                                    "nodes": [
                                        {
                                            "id": 283,
                                            "type": "CLIPTextEncode",
                                            "inputs": [
                                                {"name": "clip", "link": 904},
                                                {"name": "text", "link": None, "widget": {"name": "text"}},
                                            ],
                                            "widgets_values": ["face, hair"],
                                        }
                                    ],
                                },
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            manager = WorkflowManager(workflow_dir=temp_dir)

            fields = manager.inspect_text_prompt_fields("ui_subgraph_prompts")

            self.assertEqual([field["key"] for field in fields], ["positive_prompt", "sam3_1_prompt_section_a", "face_or_outfit_prompt"])
            self.assertEqual(fields[1]["value"], "face")
            self.assertEqual(fields[2]["graph"], "subgraph:Face or Outfit Prompt")

    def test_render_generation_workflow_applies_text_overrides_to_subgraphs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir, "ui_subgraph_prompts.json")
            workflow_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 1,
                                "type": "CLIPTextEncode",
                                "title": "Positive Prompt",
                                "inputs": [
                                    {"name": "clip", "link": 10},
                                    {"name": "text", "link": None, "widget": {"name": "text"}},
                                ],
                                "widgets_values": ["old positive"],
                            }
                        ],
                        "links": [[10, 2, 0, 1, 0, "CLIP"]],
                        "definitions": {
                            "subgraphs": [
                                {
                                    "name": "Sam3.1 Prompt Section A",
                                    "nodes": [
                                        {
                                            "id": 264,
                                            "type": "CLIPTextEncode",
                                            "inputs": [
                                                {"name": "clip", "link": 851},
                                                {"name": "text", "link": None, "widget": {"name": "text"}},
                                            ],
                                            "widgets_values": ["face"],
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            object_info = {
                "CLIPTextEncode": {
                    "input": {"required": {"text": ["STRING", {}], "clip": ["CLIP", {}]}},
                },
            }
            manager = WorkflowManager(workflow_dir=temp_dir, object_info_loader=lambda: object_info)
            workflow = manager.load_workflow("ui_subgraph_prompts")

            manager._inject_text_overrides_into_ui_workflow(  # noqa: SLF001
                workflow,
                {"sam3_1_prompt_section_a": "woman's hair"},
            )

            subgraph_node = workflow["definitions"]["subgraphs"][0]["nodes"][0]
            self.assertEqual(subgraph_node["widgets_values"][0], "woman's hair")
