import json
from django.core.files.base import ContentFile
from pathlib import Path
from tempfile import TemporaryDirectory
from django.test import TestCase, override_settings

from apps.core.models import GenerationJob, MediaAsset, TelegramUser
from apps.core.services.job_service import JobService
from apps.core.services.oddesy_agent_service import OddesyAgentService
from apps.core.services.workflow_manager import WorkflowManager


@override_settings(WORKFLOWS_DIR=Path(__file__).resolve().parents[3] / "workflows")
class OddesyAgentServiceTests(TestCase):
    def setUp(self) -> None:
        self.telegram_user = TelegramUser.objects.create(
            telegram_user_id=12345,
            username="tester",
            is_allowed=True,
        )
        self.other_user = TelegramUser.objects.create(
            telegram_user_id=67890,
            username="other",
            is_allowed=True,
        )
        self.input_media = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name="input.jpg",
            telegram_file_id="telegram-file-1",
            file=ContentFile(b"image-bytes", name="input.jpg"),
        )
        self.generated_media = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name="output.mp4",
            file=ContentFile(b"video-bytes", name="output.mp4"),
        )
        self.service = OddesyAgentService(job_service=JobService())

    def test_get_latest_input_media_returns_latest_owned_input(self) -> None:
        newer = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name="newer.jpg",
            file=ContentFile(b"newer-bytes", name="newer.jpg"),
        )

        media_asset = self.service.get_latest_input_media(self.telegram_user)

        self.assertEqual(media_asset.id, newer.id)

    def test_get_latest_generated_media_returns_latest_owned_output(self) -> None:
        newer = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="newer.png",
            file=ContentFile(b"newer-image", name="newer.png"),
        )

        media_asset = self.service.get_latest_generated_media(self.telegram_user)

        self.assertEqual(media_asset.id, newer.id)

    def test_get_latest_generated_image_returns_latest_owned_image(self) -> None:
        image_asset = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="latest.png",
            file=ContentFile(b"latest-image", name="latest.png"),
        )

        media_asset = self.service.get_latest_generated_image(self.telegram_user)

        self.assertEqual(media_asset.id, image_asset.id)

    def test_get_latest_generated_media_skips_missing_cleaned_assets(self) -> None:
        newer = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="newer.png",
            file=ContentFile(b"newer-image", name="newer.png"),
            metadata={"cleanup": {"removed_from_library": True}},
        )
        Path(newer.file.path).unlink()

        media_asset = self.service.get_latest_generated_media(self.telegram_user)

        self.assertEqual(media_asset.id, self.generated_media.id)

    def test_create_job_from_existing_media_rejects_foreign_media(self) -> None:
        foreign_media = MediaAsset.objects.create(
            telegram_user=self.other_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name="foreign.jpg",
            file=ContentFile(b"foreign", name="foreign.jpg"),
        )

        with self.assertRaises(ValueError):
            self.service.create_job_from_existing_media(
                telegram_user=self.telegram_user,
                media_asset=foreign_media,
                workflow_name="workflow_a",
                prompt="make video",
            )

    def test_create_job_from_existing_media_creates_queued_job(self) -> None:
        job = self.service.create_job_from_existing_media(
            telegram_user=self.telegram_user,
            media_asset=self.input_media,
            workflow_name="i2v_wan_480p",
            prompt="make video",
            seed=42,
            metadata={"source": "service"},
        )

        self.assertEqual(job.state, GenerationJob.STATE_QUEUED)
        self.assertEqual(job.telegram_user_id, self.telegram_user.id)
        self.assertEqual(job.input_media_id, self.input_media.id)
        self.assertEqual(job.workflow_name, "i2v_wan_480p")
        self.assertEqual(job.seed, 42)
        self.assertEqual(job.metadata["source"], "service")
        self.assertEqual(job.priority, 100)
        self.assertEqual(job.requested_executor, GenerationJob.EXECUTOR_LOCAL_GPU)

    def test_create_job_from_existing_media_rejects_unknown_workflow(self) -> None:
        with self.assertRaises(ValueError):
            self.service.create_job_from_existing_media(
                telegram_user=self.telegram_user,
                media_asset=self.input_media,
                workflow_name="workflow_missing",
                prompt="make video",
            )

    def test_create_job_from_prompt_resolves_fuzzy_workflow_name(self) -> None:
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "jugg_latent_cyberpony (1).json").write_text("{}", encoding="utf-8")
            service = OddesyAgentService(
                job_service=JobService(),
                workflow_manager=WorkflowManager(workflow_dir=temp_dir),
            )
            job = service.create_job_from_prompt(
                telegram_user=self.telegram_user,
                workflow_name="jugg_latent_cyberpony",
                prompt="cyberpunk portrait",
                seed=7,
            )

            self.assertEqual(job.state, GenerationJob.STATE_QUEUED)
            self.assertIsNone(job.input_media_id)
            self.assertEqual(job.workflow_name, "jugg_latent_cyberpony (1)")
            self.assertEqual(job.seed, 7)

    def test_set_active_text_workflow_persists_resolved_workflow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "jugg_latent_cyberpony (1).json").write_text("{}", encoding="utf-8")
            service = OddesyAgentService(
                job_service=JobService(),
                workflow_manager=WorkflowManager(workflow_dir=temp_dir),
            )

            resolved = service.set_active_text_workflow(self.telegram_user, "jugg_latent_cyberpony")

            self.telegram_user.refresh_from_db()
            self.assertEqual(resolved, "jugg_latent_cyberpony (1)")
            self.assertEqual(self.telegram_user.active_text_workflow, "jugg_latent_cyberpony (1)")

    def test_set_active_video_workflow_persists_resolved_workflow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "LTX2.3 I2V 4060 Optimised - PRN.json").write_text("{}", encoding="utf-8")
            service = OddesyAgentService(
                job_service=JobService(),
                workflow_manager=WorkflowManager(workflow_dir=temp_dir),
            )

            resolved = service.set_active_video_workflow(self.telegram_user, "LTX2.3 I2V 4060 Optimised - PRN")

            self.telegram_user.refresh_from_db()
            self.assertEqual(resolved, "LTX2.3 I2V 4060 Optimised - PRN")
            self.assertEqual(self.telegram_user.active_video_workflow, "LTX2.3 I2V 4060 Optimised - PRN")

    def test_set_image_output_mode_persists_mode(self) -> None:
        mode = self.service.set_image_output_mode(self.telegram_user, "all")

        self.telegram_user.refresh_from_db()
        self.assertEqual(mode, "all")
        self.assertEqual(self.telegram_user.image_output_mode, "all")

    def test_set_generation_batch_count_persists_count(self) -> None:
        count = self.service.set_generation_batch_count(self.telegram_user, "3")

        self.telegram_user.refresh_from_db()
        self.assertEqual(count, 3)
        self.assertEqual(self.telegram_user.generation_batch_count, 3)

    def test_set_active_video_negative_prompt_persists_prompt(self) -> None:
        prompt = self.service.set_active_video_negative_prompt(self.telegram_user, "blurry, low quality")

        self.telegram_user.refresh_from_db()
        self.assertEqual(prompt, "blurry, low quality")
        self.assertEqual(self.telegram_user.active_video_negative_prompt, "blurry, low quality")

    def test_set_active_video_length_frames_persists_value(self) -> None:
        length_frames = self.service.set_active_video_length_frames(self.telegram_user, "120")

        self.telegram_user.refresh_from_db()
        self.assertEqual(length_frames, 120)
        self.assertEqual(self.telegram_user.active_video_length_frames, 120)

    def test_set_workflow_lora_override_persists_override(self) -> None:
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "jugg_latent_cyberpony (1).json").write_text(
                '{"nodes":[{"id":59,"type":"Power Lora Loader (rgthree)","inputs":[],"widgets_values":[{},{"type":"PowerLoraLoaderHeaderWidget"},{"on":true,"lora":"pony\\\\a.safetensors","strength":1.0,"strengthTwo":null}]}],"links":[]}',
                encoding="utf-8",
            )
            service = OddesyAgentService(
                job_service=JobService(),
                workflow_manager=WorkflowManager(workflow_dir=temp_dir),
            )

            override = service.set_workflow_lora_override(
                self.telegram_user,
                "jugg_latent_cyberpony (1)",
                1,
                on=False,
                lora="pony\\override.safetensors",
                strength=0.7,
            )

            self.telegram_user.refresh_from_db()
            self.assertFalse(override["on"])
            self.assertEqual(
                self.telegram_user.workflow_lora_overrides["jugg_latent_cyberpony (1)"]["1"]["lora"],
                "pony\\override.safetensors",
            )

    def test_set_workflow_text_override_persists_override(self) -> None:
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "Flux Swap-Anything (Sam3.1).json").write_text(
                '{"nodes":[{"id":1,"type":"CLIPTextEncode","title":"Positive Prompt","inputs":[{"name":"clip","link":10},{"name":"text","link":null,"widget":{"name":"text"}}],"widgets_values":["old positive"]}],"links":[[10,2,0,1,0,"CLIP"]],"definitions":{"subgraphs":[{"name":"Face or Outfit Prompt","nodes":[{"id":283,"type":"CLIPTextEncode","inputs":[{"name":"clip","link":904},{"name":"text","link":null,"widget":{"name":"text"}}],"widgets_values":["face, hair"]}]}]}}',
                encoding="utf-8",
            )
            service = OddesyAgentService(
                job_service=JobService(),
                workflow_manager=WorkflowManager(workflow_dir=temp_dir),
            )

            value = service.set_workflow_text_override(
                self.telegram_user,
                "Flux Swap-Anything (Sam3.1)",
                "face_or_outfit_prompt",
                "outfit, accessories",
            )

            self.telegram_user.refresh_from_db()
            self.assertEqual(value, "outfit, accessories")
            self.assertEqual(
                self.telegram_user.workflow_text_overrides["Flux Swap-Anything (Sam3.1)"]["face_or_outfit_prompt"],
                "outfit, accessories",
            )

    def test_set_workflow_image_override_persists_override(self) -> None:
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "Flux Swap-Anything (Sam3.1).json").write_text(
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
                                "widgets_values": ["reference.png", "image"],
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
            service = OddesyAgentService(
                job_service=JobService(),
                workflow_manager=WorkflowManager(workflow_dir=temp_dir),
            )
            generated_image = MediaAsset.objects.create(
                telegram_user=self.telegram_user,
                asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
                original_file_name="generated.png",
                file=ContentFile(b"generated-image", name="generated.png"),
            )

            override_id = service.set_workflow_image_override(
                self.telegram_user,
                "Flux Swap-Anything (Sam3.1)",
                "face_or_outfit_referance",
                generated_image.id,
            )

            self.telegram_user.refresh_from_db()
            self.assertEqual(override_id, generated_image.id)
            self.assertEqual(
                self.telegram_user.workflow_image_overrides["Flux Swap-Anything (Sam3.1)"]["face_or_outfit_referance"],
                generated_image.id,
            )

    def test_imageswap_defaults_and_draft_persist_separately(self) -> None:
        user = TelegramUser.objects.create(
            telegram_user_id=24680,
            username="imageswapper",
            imageswap_defaults={
                "workflow_name": "Flux Swap-Anything (Sam3.1)",
                "sam_prompt_text": "hairline and jaw",
            },
            imageswap_draft={
                "target_media_asset_id": self.input_media.id,
                "positive_prompt": "editorial portrait",
                "swap_text_prompt": "swap to studio portrait",
            },
        )

        user.refresh_from_db()

        self.assertEqual(
            user.imageswap_defaults,
            {
                "workflow_name": "Flux Swap-Anything (Sam3.1)",
                "sam_prompt_text": "hairline and jaw",
            },
        )
        self.assertEqual(
            user.imageswap_draft,
            {
                "target_media_asset_id": self.input_media.id,
                "positive_prompt": "editorial portrait",
                "swap_text_prompt": "swap to studio portrait",
            },
        )

    def test_set_imageswap_defaults_normalizes_legacy_mode_to_sam_prompt_text(self) -> None:
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "Flux Swap-Anything (Sam3.1).json").write_text(
                json.dumps(
                    {
                        "nodes": [],
                        "links": [],
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
            service = OddesyAgentService(
                job_service=JobService(),
                workflow_manager=WorkflowManager(workflow_dir=temp_dir),
            )
            swap_reference = MediaAsset.objects.create(
                telegram_user=self.telegram_user,
                asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
                original_file_name="default-swap.png",
                file=ContentFile(b"default-swap", name="default-swap.png"),
            )
            self.telegram_user.imageswap_defaults = {
                "workflow_name": "flux swap-anything (sam3.1)",
                "mode": " Outfit ",
                "source_media_asset_id": self.input_media.id,
                "positive_prompt": "  editorial portrait  ",
                "prompt": "  swap to velvet jacket  ",
                "swap_media_asset_id": swap_reference.id,
            }
            self.telegram_user.save(update_fields=["imageswap_defaults", "updated_at"])

            defaults = service.get_imageswap_defaults(self.telegram_user)

            self.telegram_user.refresh_from_db()
            self.assertEqual(
                defaults,
                {
                    "workflow_name": "Flux Swap-Anything (Sam3.1)",
                    "sam_prompt_text": "Outfit",
                    "target_media_asset_id": self.input_media.id,
                    "positive_prompt": "editorial portrait",
                    "swap_text_prompt": "swap to velvet jacket",
                    "swap_reference_media_asset_id": swap_reference.id,
                },
            )
            self.assertEqual(self.telegram_user.imageswap_defaults, defaults)

    def test_set_imageswap_draft_persists_guided_flow_fields(self) -> None:
        swap_reference = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="generated-swap.png",
            file=ContentFile(b"generated-image", name="generated-swap.png"),
        )

        draft = self.service.set_imageswap_draft(
            self.telegram_user,
            sam_prompt_text=" hairline and jaw ",
            target_media_asset_id=self.input_media.id,
            positive_prompt="  editorial portrait  ",
            swap_text_prompt="  leather jacket swap  ",
            swap_reference_media_asset_id=swap_reference.id,
        )

        self.telegram_user.refresh_from_db()
        self.assertEqual(
            draft,
            {
                "sam_prompt_text": "hairline and jaw",
                "target_media_asset_id": self.input_media.id,
                "positive_prompt": "editorial portrait",
                "swap_text_prompt": "leather jacket swap",
                "swap_reference_media_asset_id": swap_reference.id,
            },
        )
        self.assertEqual(self.telegram_user.imageswap_draft, draft)

    def test_set_imageswap_draft_rejects_missing_explicit_media_override(self) -> None:
        with self.assertRaisesMessage(ValueError, "Imageswap reference image was not found."):
            self.service.set_imageswap_draft(
                self.telegram_user,
                swap_reference_media_asset_id=999999,
            )

    def test_get_imageswap_draft_normalizes_legacy_keys_and_clears_invalid_media_references(self) -> None:
        missing_source = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name="missing.png",
            file=ContentFile(b"missing-image", name="missing.png"),
        )
        Path(missing_source.file.path).unlink()
        generated_image = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
            original_file_name="generated-swap.png",
            file=ContentFile(b"generated-image", name="generated-swap.png"),
        )
        self.telegram_user.imageswap_draft = {
            "source_media_asset_id": missing_source.id,
            "swap_media_asset_id": generated_image.id,
            "prompt": "  swap to velvet jacket  ",
            "positive_prompt": "  glossy fashion photo  ",
        }
        self.telegram_user.save(update_fields=["imageswap_draft", "updated_at"])

        draft = self.service.get_imageswap_draft(self.telegram_user)

        self.telegram_user.refresh_from_db()
        self.assertEqual(
            draft,
            {
                "positive_prompt": "glossy fashion photo",
                "swap_text_prompt": "swap to velvet jacket",
                "swap_reference_media_asset_id": generated_image.id,
            },
        )
        self.assertEqual(self.telegram_user.imageswap_draft, draft)

    def test_build_imageswap_request_uses_saved_state_and_workflow_overrides(self) -> None:
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "Flux Swap-Anything (Sam3.1).json").write_text(
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
                                "widgets_values": ["reference.png", "image"],
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
                                    "name": "Sam3.1 Prompt Section B",
                                    "nodes": [
                                        {
                                            "id": 265,
                                            "type": "CLIPTextEncode",
                                            "inputs": [
                                                {"name": "clip", "link": 852},
                                                {"name": "text", "link": None, "widget": {"name": "text"}},
                                            ],
                                            "widgets_values": ["jawline"],
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
            service = OddesyAgentService(
                job_service=JobService(),
                workflow_manager=WorkflowManager(workflow_dir=temp_dir),
            )
            swap_image = MediaAsset.objects.create(
                telegram_user=self.telegram_user,
                asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
                original_file_name="swap.png",
                file=ContentFile(b"swap-image", name="swap.png"),
            )
            service.set_imageswap_defaults(
                self.telegram_user,
                workflow_name="Flux Swap-Anything (Sam3.1)",
                target_media_asset_id=self.input_media.id,
                positive_prompt="editorial beauty photo",
                swap_reference_media_asset_id=swap_image.id,
                sam_prompt_text="hairline and jaw",
            )
            service.set_imageswap_draft(
                self.telegram_user,
                swap_text_prompt="studio portrait lighting",
            )

            request = service.build_imageswap_request(self.telegram_user)

            self.assertEqual(request["workflow_name"], "Flux Swap-Anything (Sam3.1)")
            self.assertEqual(request["target_media_asset"].id, self.input_media.id)
            self.assertEqual(request["target_media_asset_id"], self.input_media.id)
            self.assertEqual(request["swap_reference_media_asset"].id, swap_image.id)
            self.assertEqual(request["swap_reference_media_asset_id"], swap_image.id)
            self.assertEqual(request["sam_prompt_text"], "hairline and jaw")
            self.assertEqual(request["positive_prompt"], "editorial beauty photo")
            self.assertEqual(request["face_or_outfit_prompt"], "studio portrait lighting")
            self.assertEqual(request["job_payload"]["media_asset"].id, self.input_media.id)
            self.assertEqual(request["job_payload"]["prompt"], "editorial beauty photo")
            self.assertEqual(
                request["job_payload"]["metadata"]["parsed_instruction"]["image_overrides"],
                {"face_or_outfit_referance": swap_image.id},
            )
            self.assertEqual(
                request["job_payload"]["metadata"]["parsed_instruction"]["text_overrides"],
                {
                    "sam3_1_prompt_section_a": "hairline and jaw",
                    "sam3_1_prompt_section_b": "hairline and jaw",
                    "face_or_outfit_prompt": "studio portrait lighting",
                },
            )
            self.assertEqual(
                request["job_payload"]["metadata"]["imageswap"],
                {
                    "workflow_name": "Flux Swap-Anything (Sam3.1)",
                    "target_media_asset_id": self.input_media.id,
                    "sam_prompt_text": "hairline and jaw",
                    "positive_prompt": "editorial beauty photo",
                    "face_or_outfit_prompt": "studio portrait lighting",
                    "swap_reference_media_asset_id": swap_image.id,
                },
            )

    def test_build_imageswap_request_requires_all_first_version_fields(self) -> None:
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "Flux Swap-Anything (Sam3.1).json").write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 215,
                                "type": "LoadImage",
                                "title": "Face or Outfit Referance",
                                "inputs": [
                                    {"name": "image", "link": None, "widget": {"name": "image"}},
                                    {"name": "upload", "link": None, "widget": {"name": "upload"}},
                                ],
                                "widgets_values": ["swap.png", "image"],
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
                                {
                                    "name": "I2I Prompt",
                                    "nodes": [
                                        {
                                            "id": 311,
                                            "type": "CLIPTextEncode",
                                            "inputs": [
                                                {"name": "clip", "link": 905},
                                                {"name": "text", "link": None, "widget": {"name": "text"}},
                                            ],
                                            "widgets_values": ["default i2i"],
                                        }
                                    ],
                                },
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            service = OddesyAgentService(
                job_service=JobService(),
                workflow_manager=WorkflowManager(workflow_dir=temp_dir),
            )

            service.set_imageswap_defaults(
                self.telegram_user,
                workflow_name="Flux Swap-Anything (Sam3.1)",
            )
            service.set_imageswap_draft(
                self.telegram_user,
                sam_prompt_text="hairline and jaw",
                target_media_asset_id=self.input_media.id,
                positive_prompt="editorial beauty photo",
                swap_text_prompt="studio portrait lighting",
            )

            with self.assertRaisesMessage(ValueError, "Imageswap Face or Outfit Referance is not set."):
                service.build_imageswap_request(self.telegram_user)

    def test_build_imageswap_request_reuses_mask_identifier_when_i2i_field_is_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "Flux Swap-Anything (Sam3.1).json").write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 215,
                                "type": "LoadImage",
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
            service = OddesyAgentService(
                job_service=JobService(),
                workflow_manager=WorkflowManager(workflow_dir=temp_dir),
            )
            swap_image = MediaAsset.objects.create(
                telegram_user=self.telegram_user,
                asset_type=MediaAsset.TYPE_GENERATED_IMAGE,
                original_file_name="swap.png",
                file=ContentFile(b"swap-image", name="swap.png"),
            )
            service.set_imageswap_defaults(
                self.telegram_user,
                workflow_name="Flux Swap-Anything (Sam3.1)",
                target_media_asset_id=self.input_media.id,
                positive_prompt="editorial beauty photo",
                swap_reference_media_asset_id=swap_image.id,
                sam_prompt_text="hairline and jaw",
            )
            service.set_imageswap_draft(
                self.telegram_user,
                swap_text_prompt="hairline and jaw",
            )

            request = service.build_imageswap_request(self.telegram_user)

            self.assertEqual(request["sam_prompt_text"], "hairline and jaw")
            self.assertEqual(request["face_or_outfit_prompt"], "hairline and jaw")
            self.assertEqual(
                request["job_payload"]["metadata"]["parsed_instruction"]["text_overrides"],
                {
                    "sam3_1_prompt_section_a": "hairline and jaw",
                    "face_or_outfit_prompt": "hairline and jaw",
                },
            )

    def test_get_job_status_payload_returns_owned_job_payload(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            workflow_name="workflow_b",
            prompt="status prompt",
        )

        payload = self.service.get_job_status_payload(self.telegram_user, job.id)

        self.assertEqual(payload["id"], job.id)
        self.assertEqual(payload["workflow_name"], "workflow_b")
        self.assertEqual(payload["input_media_id"], self.input_media.id)
        self.assertEqual(payload["priority"], job.priority)
        self.assertEqual(payload["requested_executor"], job.requested_executor)

    def test_list_media_payloads_filters_by_asset_type(self) -> None:
        payloads = self.service.list_media_payloads(
            self.telegram_user,
            asset_types=[MediaAsset.TYPE_GENERATED_VIDEO],
            limit=10,
        )

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["id"], self.generated_media.id)
        self.assertEqual(payloads[0]["asset_type"], MediaAsset.TYPE_GENERATED_VIDEO)

    def test_get_generated_output_payload_returns_job_output(self) -> None:
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            output_media=self.generated_media,
            workflow_name="workflow_c",
            prompt="output prompt",
        )

        payload = self.service.get_generated_output_payload(self.telegram_user, job.id)

        self.assertEqual(payload["id"], self.generated_media.id)
        self.assertEqual(payload["original_file_name"], "output.mp4")

    def test_get_generated_output_payload_returns_none_for_missing_cleaned_output(self) -> None:
        self.generated_media.metadata["cleanup"] = {"removed_from_library": True}
        self.generated_media.save(update_fields=["metadata"])
        Path(self.generated_media.file.path).unlink()
        job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            output_media=self.generated_media,
            workflow_name="workflow_c",
            prompt="output prompt",
        )

        payload = self.service.get_generated_output_payload(self.telegram_user, job.id)

        self.assertIsNone(payload)

    def test_combine_videos_by_job_ids_uses_job_output_videos(self) -> None:
        second_video = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name="output-2.mp4",
            file=ContentFile(b"video-2", name="output-2.mp4"),
        )
        job_one = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            output_media=self.generated_media,
            workflow_name="workflow_a",
            prompt="video one",
        )
        job_two = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            output_media=second_video,
            workflow_name="workflow_b",
            prompt="video two",
        )
        captured = {}

        def fake_combine_videos(user, assets):
            captured["user_id"] = user.id
            captured["asset_ids"] = [asset.id for asset in assets]
            return self.generated_media

        self.service.combine_videos = fake_combine_videos

        combined = self.service.combine_videos_by_job_ids(self.telegram_user, [job_one.id, job_two.id])

        self.assertEqual(combined.id, self.generated_media.id)
        self.assertEqual(captured["user_id"], self.telegram_user.id)
        self.assertEqual(captured["asset_ids"], [self.generated_media.id, second_video.id])

    def test_queue_last_frame_upscale_job_creates_input_asset_and_job(self) -> None:
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "Oddesy Last Frame Upscale.json").write_text(
                '{"1":{"class_type":"LoadImage","inputs":{"image":"{INPUT_IMAGE}"}},"2":{"class_type":"UpscaleModelLoader","inputs":{"model_name":"RealESRGAN_x2plus.pth"}},"3":{"class_type":"ImageUpscaleWithModel","inputs":{"upscale_model":["2",0],"image":["1",0]}},"4":{"class_type":"SaveImage","inputs":{"images":["3",0],"filename_prefix":"oddesy_lastframe_upscale"}}}',
                encoding="utf-8",
            )
            service = OddesyAgentService(
                job_service=JobService(),
                workflow_manager=WorkflowManager(workflow_dir=temp_dir),
            )
            source_video = MediaAsset.objects.create(
                telegram_user=self.telegram_user,
                asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
                original_file_name="clip.mp4",
                file=ContentFile(b"video-bytes", name="clip.mp4"),
            )

            def extract_last_frame(**kwargs):
                output_path = Path(kwargs["output_path"])
                output_path.write_bytes(b"frame-bytes")
                return {"video_path": kwargs["video_path"], "output_path": str(output_path), "frame_count": 42}

            service.video_last_frame_enhancement_service.extract_last_frame = extract_last_frame

            job = service.queue_last_frame_upscale_job(self.telegram_user, source_video.id)

            self.assertEqual(job.workflow_name, "Oddesy Last Frame Upscale")
            self.assertEqual(job.prompt, "")
            self.assertIsNotNone(job.input_media)
            self.assertEqual(job.input_media.asset_type, MediaAsset.TYPE_INCOMING_IMAGE)
            self.assertEqual(job.metadata["last_frame_upscale"]["source_video_media_asset_id"], source_video.id)

    def test_enhance_latest_video_last_frame_uses_latest_saved_uploaded_video(self) -> None:
        uploaded_video = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_VIDEO,
            original_file_name="uploaded.mp4",
            file=ContentFile(b"uploaded-video", name="uploaded.mp4"),
        )
        captured = {}

        def fake_enhance_last_frame(**kwargs):
            captured["video_path"] = kwargs["video_path"]
            output_path = Path(kwargs["output_path"])
            output_path.write_bytes(b"frame-bytes")
            return {"output_path": str(output_path)}

        self.service.video_last_frame_enhancement_service.enhance_last_frame = fake_enhance_last_frame

        media_asset = self.service.enhance_latest_video_last_frame(self.telegram_user)

        self.assertIsNotNone(media_asset)
        self.assertEqual(captured["video_path"], uploaded_video.file.path)
        self.assertEqual(media_asset.asset_type, MediaAsset.TYPE_GENERATED_IMAGE)

    def test_enhance_video_last_frame_by_id_accepts_uploaded_video(self) -> None:
        uploaded_video = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_VIDEO,
            original_file_name="uploaded-id.mp4",
            file=ContentFile(b"uploaded-video", name="uploaded-id.mp4"),
        )
        captured = {}

        def fake_enhance_last_frame(**kwargs):
            captured["video_path"] = kwargs["video_path"]
            output_path = Path(kwargs["output_path"])
            output_path.write_bytes(b"frame-bytes")
            return {"output_path": str(output_path)}

        self.service.video_last_frame_enhancement_service.enhance_last_frame = fake_enhance_last_frame

        media_asset = self.service.enhance_video_last_frame_by_id(self.telegram_user, uploaded_video.id)

        self.assertIsNotNone(media_asset)
        self.assertEqual(captured["video_path"], uploaded_video.file.path)
        self.assertEqual(media_asset.asset_type, MediaAsset.TYPE_GENERATED_IMAGE)
