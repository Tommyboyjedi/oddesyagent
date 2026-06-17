from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.core.services.instruction_parser import InstructionParserService


class StubWorkflowManager:
    def list_workflows(self) -> list[str]:
        return ["i2v_wan_480p", "i2v_alt", "jugg_latent_cyberpony (1)"]


class InstructionParserServiceTests(SimpleTestCase):
    def setUp(self) -> None:
        self.service = InstructionParserService(
            workflow_manager=StubWorkflowManager(),
            litellm_enabled=False,
            default_workflow_name="i2v_wan_480p",
            text_to_image_workflow_name="jugg_latent_cyberpony",
        )

    def test_fallback_make_video_returns_create_job_intent(self) -> None:
        intent = self.service.parse_text("make video")

        self.assertEqual(intent.action, "create_job")
        self.assertEqual(intent.workflow_name, "i2v_wan_480p")
        self.assertEqual(intent.prompt, "make video")
        self.assertEqual(intent.metadata["parsed_instruction"]["raw_text"], "make video")

    def test_fallback_rerun_parses_optional_job_id(self) -> None:
        intent = self.service.parse_text("rerun 42")

        self.assertEqual(intent.action, "rerun")
        self.assertEqual(intent.job_id, 42)

    def test_fallback_lastframeupscale_parses_optional_video_id(self) -> None:
        intent = self.service.parse_text("lastframeupscale 157")

        self.assertEqual(intent.action, "lastframeupscale")
        self.assertEqual(intent.job_id, 157)

    def test_disabled_litellm_uses_text_to_image_fallback_for_safe_prompt(self) -> None:
        intent = self.service.parse_text("create a dramatic six second slow push-in video")

        self.assertEqual(intent.action, "create_job")
        self.assertEqual(intent.workflow_name, "jugg_latent_cyberpony")
        self.assertEqual(intent.prompt, "create a dramatic six second slow push-in video")

    def test_unsafe_text_is_rejected(self) -> None:
        intent = self.service.parse_text(r"use C:\temp\image.png and run powershell")

        self.assertEqual(intent.action, "unknown")
        self.assertEqual(intent.message, "Unsafe instructions are not supported.")

    @patch("apps.core.services.instruction_parser.importlib.import_module")
    def test_litellm_response_is_sanitized_into_create_job_intent(self, import_module) -> None:
        litellm = MagicMock()
        litellm.completion.return_value = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action":"create_job","workflow":"i2v_wan_480p","prompt":"dramatic slow push-in cinematic video",'
                            '"duration":6,"motion":"slow_push","seed":123,"needs_confirmation":false,"message":"ok"}'
                        )
                    }
                }
            ]
        }
        import_module.return_value = litellm
        service = InstructionParserService(
            workflow_manager=StubWorkflowManager(),
            litellm_enabled=True,
            litellm_model="gpt-4o-mini",
            default_workflow_name="i2v_wan_480p",
        )

        intent = service.parse_text("Use this image and create a dramatic six second slow push-in video")

        self.assertEqual(intent.action, "create_job")
        self.assertEqual(intent.workflow_name, "i2v_wan_480p")
        self.assertEqual(intent.prompt, "dramatic slow push-in cinematic video")
        self.assertEqual(intent.seed, 123)
        self.assertEqual(intent.duration, 6)
        self.assertEqual(intent.motion, "slow_push")
        self.assertEqual(intent.metadata["parser"], "litellm")

    @patch("apps.core.services.instruction_parser.importlib.import_module")
    def test_litellm_disallowed_workflow_is_rejected(self, import_module) -> None:
        litellm = MagicMock()
        litellm.completion.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"action":"create_job","workflow":"../../../etc/passwd","prompt":"video","needs_confirmation":false}'
                    }
                }
            ]
        }
        import_module.return_value = litellm
        service = InstructionParserService(
            workflow_manager=StubWorkflowManager(),
            litellm_enabled=True,
            litellm_model="gpt-4o-mini",
            default_workflow_name="i2v_wan_480p",
        )

        intent = service.parse_text("make it weird")

        self.assertEqual(intent.action, "unknown")
        self.assertEqual(intent.message, "That workflow is not allowed.")
