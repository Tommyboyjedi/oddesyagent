from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from apps.core.models import GenerationJob, MediaAsset, TelegramUser, ToolDefinition, ToolExecutionRequest
from apps.core.services.tool_registry import ToolRegistryService


@override_settings(ODDESY_SAFE_ROOTS=[r"C:\safe-root"], VAST_API_KEY="", YOUTUBE_UPLOAD_ENABLED=False)
class ToolRegistryServiceTests(TestCase):
    def setUp(self) -> None:
        self.service = ToolRegistryService()

    def test_register_tool_persists_policy_fields(self) -> None:
        tool = self.service.register_tool(
            name="media_cleanup",
            description="Clean generated media inside a safe root.",
            allowed_inputs=["target_path", "dry_run"],
            forbidden_inputs=["force_delete"],
            audit_requirements=["record operator id", "record affected files"],
            safe_roots=[r"C:\safe-root"],
            is_enabled=True,
        )

        self.assertEqual(tool.allowed_inputs, ["target_path", "dry_run"])
        self.assertEqual(tool.forbidden_inputs, ["force_delete"])
        self.assertEqual(tool.audit_requirements, ["record operator id", "record affected files"])
        self.assertEqual(tool.safe_roots, [r"C:\safe-root"])

    def test_evaluate_request_rejects_unknown_input_key(self) -> None:
        self.service.register_tool(
            name="media_cleanup",
            description="Clean generated media inside a safe root.",
            allowed_inputs=["target_path"],
            forbidden_inputs=[],
            audit_requirements=[],
            safe_roots=[r"C:\safe-root"],
            is_enabled=True,
        )

        decision = self.service.evaluate_request(
            "media_cleanup",
            {"target_path": r"C:\safe-root\job-1", "unexpected": True},
        )

        self.assertEqual(decision.status, "rejected")
        self.assertIn("unexpected", decision.message)

    def test_evaluate_request_rejects_forbidden_input_key(self) -> None:
        self.service.register_tool(
            name="media_cleanup",
            description="Clean generated media inside a safe root.",
            allowed_inputs=["target_path", "force_delete"],
            forbidden_inputs=["force_delete"],
            audit_requirements=[],
            safe_roots=[r"C:\safe-root"],
            is_enabled=True,
        )

        decision = self.service.evaluate_request(
            "media_cleanup",
            {"target_path": r"C:\safe-root\job-1", "force_delete": True},
        )

        self.assertEqual(decision.status, "rejected")
        self.assertIn("forbids inputs: force_delete", decision.message)

    def test_evaluate_request_rejects_path_outside_safe_root(self) -> None:
        self.service.register_tool(
            name="nas_browser",
            description="Browse NAS media.",
            allowed_inputs=["target_path"],
            forbidden_inputs=[],
            audit_requirements=[],
            safe_roots=[r"C:\safe-root"],
            is_enabled=True,
            metadata={"provider": "nas"},
        )

        decision = self.service.evaluate_request(
            "nas_browser",
            {"target_path": r"C:\other-root\private"},
        )

        self.assertEqual(decision.status, "rejected")
        self.assertIn("configured safe roots", decision.message)

    def test_evaluate_request_rejects_wildcard_input(self) -> None:
        self.service.register_tool(
            name="media_cleanup",
            description="Clean generated media inside a safe root.",
            allowed_inputs=["target_path"],
            forbidden_inputs=[],
            audit_requirements=[],
            safe_roots=[r"C:\safe-root"],
            is_enabled=True,
        )

        decision = self.service.evaluate_request(
            "media_cleanup",
            {"target_path": r"C:\safe-root\*.mp4"},
        )

        self.assertEqual(decision.status, "rejected")
        self.assertEqual(decision.message, "Tool 'media_cleanup' rejects wildcard inputs.")

    def test_evaluate_request_rejects_external_tool_without_configuration(self) -> None:
        self.service.register_tool(
            name="vast_management",
            description="Manage Vast.ai instances.",
            allowed_inputs=["instance_id"],
            forbidden_inputs=[],
            audit_requirements=[],
            is_external=True,
            is_enabled=True,
            metadata={"provider": "vast"},
        )

        decision = self.service.evaluate_request("vast_management", {"instance_id": "abc"})

        self.assertEqual(decision.status, "rejected")
        self.assertEqual(decision.message, "Vast.ai access is not configured.")

    def test_evaluate_request_requires_confirmation_for_destructive_tool(self) -> None:
        self.service.register_tool(
            name="media_cleanup",
            description="Clean generated media inside a safe root.",
            allowed_inputs=["target_path"],
            forbidden_inputs=[],
            audit_requirements=[],
            is_destructive=True,
            safe_roots=[r"C:\safe-root"],
            is_enabled=True,
        )

        decision = self.service.evaluate_request(
            "media_cleanup",
            {"target_path": r"C:\safe-root\job-1"},
        )

        self.assertEqual(decision.status, "awaiting_confirmation")
        self.assertTrue(decision.requires_confirmation)

    def test_evaluate_request_approves_safe_local_tool(self) -> None:
        self.service.register_tool(
            name="media_cleanup_preview",
            description="Preview generated media cleanup inside a safe root.",
            allowed_inputs=["target_path", "dry_run"],
            forbidden_inputs=[],
            audit_requirements=[],
            safe_roots=[r"C:\safe-root"],
            is_enabled=True,
        )

        decision = self.service.evaluate_request(
            "media_cleanup_preview",
            {"target_path": r"C:\safe-root\job-1", "dry_run": True},
        )

        self.assertEqual(decision.status, "approved")
        self.assertEqual(decision.message, "Tool 'media_cleanup_preview' is allowed.")

    def test_log_tool_decision_records_audit_event(self) -> None:
        decision = self.service.evaluate_request("missing_tool", {})
        audit_log = self.service.log_tool_decision(
            tool_name="missing_tool",
            decision=decision,
            requested_inputs={},
        )

        self.assertEqual(audit_log.event_type, "tool_registry")
        self.assertEqual(audit_log.metadata["tool_name"], "missing_tool")
        self.assertEqual(audit_log.metadata["decision"], "rejected")

    def test_submit_request_tracks_awaiting_confirmation_state(self) -> None:
        self.service.register_tool(
            name="media_cleanup",
            description="Clean generated media inside a safe root.",
            allowed_inputs=["target_path"],
            forbidden_inputs=[],
            audit_requirements=[],
            is_destructive=True,
            safe_roots=[r"C:\safe-root"],
            is_enabled=True,
        )

        request = self.service.submit_request(
            tool_name="media_cleanup",
            requested_inputs={"target_path": r"C:\safe-root\job-1"},
        )

        self.assertEqual(request.status, ToolExecutionRequest.STATUS_AWAITING_CONFIRMATION)
        self.assertTrue(request.requires_confirmation)

    def test_confirm_request_marks_approved(self) -> None:
        self.service.register_tool(
            name="media_cleanup",
            description="Clean generated media inside a safe root.",
            allowed_inputs=["target_path"],
            forbidden_inputs=[],
            audit_requirements=[],
            is_destructive=True,
            safe_roots=[r"C:\safe-root"],
            is_enabled=True,
        )
        request = self.service.submit_request(
            tool_name="media_cleanup",
            requested_inputs={"target_path": r"C:\safe-root\job-1"},
        )

        confirmed = self.service.confirm_request(request.id)

        self.assertEqual(confirmed.status, ToolExecutionRequest.STATUS_APPROVED)
        self.assertIn("explicitly confirmed", confirmed.decision_message)

    def test_reject_request_marks_rejected(self) -> None:
        self.service.register_tool(
            name="media_cleanup",
            description="Clean generated media inside a safe root.",
            allowed_inputs=["target_path"],
            forbidden_inputs=[],
            audit_requirements=[],
            is_destructive=True,
            safe_roots=[r"C:\safe-root"],
            is_enabled=True,
        )
        request = self.service.submit_request(
            tool_name="media_cleanup",
            requested_inputs={"target_path": r"C:\safe-root\job-1"},
        )

        rejected = self.service.reject_request(request.id, reason="Operator denied")

        self.assertEqual(rejected.status, ToolExecutionRequest.STATUS_REJECTED)
        self.assertEqual(rejected.decision_message, "Operator denied")

    def test_execute_request_runs_safe_root_browser_tool(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "clip.mp4").write_bytes(b"video")
            self.service.register_tool(
                name="safe_root_browser",
                description="Browse files inside a safe root.",
                allowed_inputs=["target_path", "recursive", "limit"],
                forbidden_inputs=[],
                audit_requirements=[],
                safe_roots=[str(root)],
                is_enabled=True,
                metadata={"provider": "nas", "executor": "safe_root_browser"},
            )

            request = self.service.submit_request(
                tool_name="safe_root_browser",
                requested_inputs={"target_path": str(root), "limit": 10},
            )
            executed = self.service.execute_request(request.id)

            self.assertEqual(executed.status, ToolExecutionRequest.STATUS_EXECUTED)
            self.assertEqual(executed.metadata["execution_result"]["count"], 1)
            self.assertEqual(executed.metadata["execution_result"]["entries"][0]["name"], "clip.mp4")

    def test_execute_request_runs_media_cleanup_preview_tool(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "clip.mp4").write_bytes(b"video")
            (root / "notes.txt").write_text("ignore", encoding="utf-8")
            self.service.register_tool(
                name="media_cleanup_preview",
                description="Preview media cleanup candidates inside a safe root.",
                allowed_inputs=["target_path", "recursive", "limit", "older_than_days", "extensions"],
                forbidden_inputs=[],
                audit_requirements=[],
                safe_roots=[str(root)],
                is_enabled=True,
                metadata={"provider": "nas", "executor": "media_cleanup_preview"},
            )

            request = self.service.submit_request(
                tool_name="media_cleanup_preview",
                requested_inputs={"target_path": str(root), "limit": 10},
            )
            executed = self.service.execute_request(request.id)

            self.assertEqual(executed.status, ToolExecutionRequest.STATUS_EXECUTED)
            self.assertEqual(executed.metadata["execution_result"]["candidate_count"], 1)
            self.assertEqual(executed.metadata["execution_result"]["candidates"][0]["name"], "clip.mp4")

    def test_execute_request_runs_confirmed_media_cleanup_tool(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_video = root / "clip.mp4"
            old_video.write_bytes(b"video")
            self.service.register_tool(
                name="media_cleanup",
                description="Delete previewed media cleanup candidates inside a safe root.",
                allowed_inputs=["target_path", "recursive", "limit", "older_than_days", "extensions"],
                forbidden_inputs=[],
                audit_requirements=[],
                safe_roots=[str(root)],
                is_enabled=True,
                is_destructive=True,
                metadata={"provider": "nas", "executor": "media_cleanup"},
            )

            request = self.service.submit_request(
                tool_name="media_cleanup",
                requested_inputs={"target_path": str(root), "limit": 10},
            )
            approved = self.service.confirm_request(request.id)
            executed = self.service.execute_request(approved.id)

            self.assertEqual(executed.status, ToolExecutionRequest.STATUS_EXECUTED)
            self.assertEqual(executed.metadata["execution_result"]["deleted_count"], 1)
            self.assertFalse(old_video.exists())

    def test_execute_request_runs_media_library_report_tool(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with override_settings(MEDIA_ROOT=temp_dir, ODDESY_SAFE_ROOTS=[temp_dir], VAST_API_KEY="", YOUTUBE_UPLOAD_ENABLED=False):
                telegram_user = TelegramUser.objects.create(
                    telegram_user_id=777,
                    username="tester",
                    is_allowed=True,
                )
                input_media = MediaAsset.objects.create(
                    telegram_user=telegram_user,
                    asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
                    original_file_name="input.jpg",
                    file=ContentFile(b"image", name="input.jpg"),
                )
                generated_media = MediaAsset.objects.create(
                    telegram_user=telegram_user,
                    asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
                    original_file_name="output.mp4",
                    file=ContentFile(b"video", name="output.mp4"),
                )
                job = GenerationJob.objects.create(
                    telegram_user=telegram_user,
                    input_media=input_media,
                    output_media=generated_media,
                    workflow_name="workflow_a",
                    prompt="prompt",
                )
                job.mark_completed(generated_media)
                self.service.register_tool(
                    name="media_library_report",
                    description="Report generated media candidates from database records.",
                    allowed_inputs=["older_than_days", "asset_types", "include_missing_files", "limit"],
                    forbidden_inputs=[],
                    audit_requirements=[],
                    safe_roots=[temp_dir],
                    is_enabled=True,
                    metadata={"provider": "nas", "executor": "media_library_report"},
                )

                request = self.service.submit_request(
                    tool_name="media_library_report",
                    requested_inputs={"limit": 10},
                )
                executed = self.service.execute_request(request.id)

            self.assertEqual(executed.status, ToolExecutionRequest.STATUS_EXECUTED)
            self.assertEqual(executed.metadata["execution_result"]["candidate_count"], 1)
            self.assertEqual(executed.metadata["execution_result"]["candidates"][0]["output_job_ids"], [job.id])

    def test_execute_request_runs_confirmed_media_library_cleanup_tool(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with override_settings(MEDIA_ROOT=temp_dir, ODDESY_SAFE_ROOTS=[temp_dir], VAST_API_KEY="", YOUTUBE_UPLOAD_ENABLED=False):
                telegram_user = TelegramUser.objects.create(
                    telegram_user_id=778,
                    username="tester",
                    is_allowed=True,
                )
                input_media = MediaAsset.objects.create(
                    telegram_user=telegram_user,
                    asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
                    original_file_name="input.jpg",
                    file=ContentFile(b"image", name="input.jpg"),
                )
                generated_media = MediaAsset.objects.create(
                    telegram_user=telegram_user,
                    asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
                    original_file_name="output.mp4",
                    file=ContentFile(b"video", name="output.mp4"),
                )
                job = GenerationJob.objects.create(
                    telegram_user=telegram_user,
                    input_media=input_media,
                    output_media=generated_media,
                    workflow_name="workflow_a",
                    prompt="prompt",
                )
                job.mark_completed(generated_media)
                self.service.register_tool(
                    name="media_library_cleanup",
                    description="Delete generated media candidates from database records.",
                    allowed_inputs=["older_than_days", "asset_types", "include_missing_files", "limit"],
                    forbidden_inputs=[],
                    audit_requirements=[],
                    safe_roots=[temp_dir],
                    is_enabled=True,
                    is_destructive=True,
                    metadata={"provider": "nas", "executor": "media_library_cleanup"},
                )

                request = self.service.submit_request(
                    tool_name="media_library_cleanup",
                    requested_inputs={"limit": 10},
                )
                approved = self.service.confirm_request(request.id)
                executed = self.service.execute_request(approved.id)
                generated_media.refresh_from_db()

            self.assertEqual(executed.status, ToolExecutionRequest.STATUS_EXECUTED)
            self.assertEqual(executed.metadata["execution_result"]["deleted_asset_ids"], [generated_media.id])
            self.assertEqual(generated_media.metadata["cleanup"]["output_job_ids"], [job.id])


class ToolDefinitionModelTests(TestCase):
    def test_string_representation_reflects_enabled_state(self) -> None:
        tool = ToolDefinition.objects.create(
            name="media_cleanup",
            description="Clean generated media inside a safe root.",
        )

        self.assertEqual(str(tool), "tool:media_cleanup:disabled")


class ToolExecutionRequestModelTests(TestCase):
    def test_string_representation_includes_tool_and_status(self) -> None:
        tool = ToolDefinition.objects.create(
            name="media_cleanup",
            description="Clean generated media inside a safe root.",
        )
        request = ToolExecutionRequest.objects.create(tool=tool)

        self.assertEqual(str(request), f"tool_request:{request.id}:media_cleanup:pending")
