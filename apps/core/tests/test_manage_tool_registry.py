from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from apps.core.models import GenerationJob, MediaAsset, TelegramUser, ToolExecutionRequest
from apps.core.services.tool_registry import ToolRegistryService


class ManageToolRegistryCommandTests(TestCase):
    def setUp(self) -> None:
        self.service = ToolRegistryService()
        self.service.register_tool(
            name="media_cleanup_preview",
            description="Preview generated media cleanup inside a safe root.",
            allowed_inputs=["target_path", "dry_run"],
            forbidden_inputs=[],
            audit_requirements=["record operator id"],
            safe_roots=[r"C:\safe-root"],
            is_enabled=True,
        )
        self.service.register_tool(
            name="media_cleanup",
            description="Clean generated media inside a safe root.",
            allowed_inputs=["target_path"],
            forbidden_inputs=[],
            audit_requirements=["record operator id"],
            safe_roots=[r"C:\safe-root"],
            is_enabled=True,
            is_destructive=True,
        )

    def test_lists_enabled_tools(self) -> None:
        buffer = StringIO()

        call_command("manage_tool_registry", stdout=buffer)

        output = buffer.getvalue()
        self.assertIn("media_cleanup_preview", output)
        self.assertIn("media_cleanup", output)

    def test_submit_safe_request_is_auto_approved(self) -> None:
        buffer = StringIO()

        call_command(
            "manage_tool_registry",
            submit="media_cleanup_preview",
            inputs='{"target_path":"C:\\\\safe-root\\\\job-1","dry_run":true}',
            stdout=buffer,
        )

        request = ToolExecutionRequest.objects.get(tool__name="media_cleanup_preview")
        self.assertEqual(request.status, ToolExecutionRequest.STATUS_APPROVED)
        self.assertIn("status 'approved'", buffer.getvalue())

    def test_submit_destructive_request_waits_for_confirmation(self) -> None:
        buffer = StringIO()

        call_command(
            "manage_tool_registry",
            submit="media_cleanup",
            inputs='{"target_path":"C:\\\\safe-root\\\\job-2"}',
            stdout=buffer,
        )

        request = ToolExecutionRequest.objects.get(tool__name="media_cleanup")
        self.assertEqual(request.status, ToolExecutionRequest.STATUS_AWAITING_CONFIRMATION)
        self.assertIn("status 'awaiting_confirmation'", buffer.getvalue())

    def test_confirm_request_transitions_to_approved(self) -> None:
        request = self.service.submit_request(
            tool_name="media_cleanup",
            requested_inputs={"target_path": r"C:\safe-root\job-3"},
        )
        buffer = StringIO()

        call_command("manage_tool_registry", confirm=request.id, stdout=buffer)

        request.refresh_from_db()
        self.assertEqual(request.status, ToolExecutionRequest.STATUS_APPROVED)
        self.assertIn("Confirmed tool request", buffer.getvalue())

    def test_reject_request_transitions_to_rejected(self) -> None:
        request = self.service.submit_request(
            tool_name="media_cleanup",
            requested_inputs={"target_path": r"C:\safe-root\job-4"},
        )
        buffer = StringIO()

        call_command("manage_tool_registry", reject=request.id, reason="Not now", stdout=buffer)

        request.refresh_from_db()
        self.assertEqual(request.status, ToolExecutionRequest.STATUS_REJECTED)
        self.assertEqual(request.decision_message, "Not now")

    def test_execute_request_prints_safe_root_browser_result(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "clip.mp4").write_bytes(b"video")
            self.service.register_tool(
                name="safe_root_browser",
                description="Browse files inside a safe root.",
                allowed_inputs=["target_path", "recursive", "limit"],
                forbidden_inputs=[],
                audit_requirements=["record operator id"],
                safe_roots=[str(root)],
                is_enabled=True,
                metadata={"provider": "nas", "executor": "safe_root_browser"},
            )
            request = self.service.submit_request(
                tool_name="safe_root_browser",
                requested_inputs={"target_path": str(root), "limit": 10},
            )
            buffer = StringIO()

            call_command("manage_tool_registry", execute=request.id, stdout=buffer)

            request.refresh_from_db()
            self.assertEqual(request.status, ToolExecutionRequest.STATUS_EXECUTED)
            output = buffer.getvalue()
            self.assertIn("Executed tool request", output)
            self.assertIn('"count": 1', output)

    def test_execute_request_prints_cleanup_preview_result(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "clip.mp4").write_bytes(b"video")
            (root / "notes.txt").write_text("ignore", encoding="utf-8")
            self.service.register_tool(
                name="media_cleanup_preview",
                description="Preview media cleanup candidates inside a safe root.",
                allowed_inputs=["target_path", "recursive", "limit", "older_than_days", "extensions"],
                forbidden_inputs=[],
                audit_requirements=["record operator id"],
                safe_roots=[str(root)],
                is_enabled=True,
                metadata={"provider": "nas", "executor": "media_cleanup_preview"},
            )
            request = self.service.submit_request(
                tool_name="media_cleanup_preview",
                requested_inputs={"target_path": str(root), "limit": 10},
            )
            buffer = StringIO()

            call_command("manage_tool_registry", execute=request.id, stdout=buffer)

            request.refresh_from_db()
            self.assertEqual(request.status, ToolExecutionRequest.STATUS_EXECUTED)
            output = buffer.getvalue()
            self.assertIn("Executed tool request", output)
            self.assertIn('"candidate_count": 1', output)

    def test_execute_request_prints_confirmed_cleanup_result(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_video = root / "clip.mp4"
            old_video.write_bytes(b"video")
            self.service.register_tool(
                name="media_cleanup",
                description="Delete previewed media cleanup candidates inside a safe root.",
                allowed_inputs=["target_path", "recursive", "limit", "older_than_days", "extensions"],
                forbidden_inputs=[],
                audit_requirements=["record operator id"],
                safe_roots=[str(root)],
                is_enabled=True,
                is_destructive=True,
                metadata={"provider": "nas", "executor": "media_cleanup"},
            )
            request = self.service.submit_request(
                tool_name="media_cleanup",
                requested_inputs={"target_path": str(root), "limit": 10},
            )
            call_command("manage_tool_registry", confirm=request.id)
            buffer = StringIO()

            call_command("manage_tool_registry", execute=request.id, stdout=buffer)

            request.refresh_from_db()
            self.assertEqual(request.status, ToolExecutionRequest.STATUS_EXECUTED)
            output = buffer.getvalue()
            self.assertIn("Executed tool request", output)
            self.assertIn('"deleted_count": 1', output)
            self.assertFalse(old_video.exists())

    def test_execute_request_prints_media_library_report_result(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with override_settings(MEDIA_ROOT=temp_dir, ODDESY_SAFE_ROOTS=[temp_dir]):
                telegram_user = TelegramUser.objects.create(
                    telegram_user_id=555,
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
                    audit_requirements=["record operator id"],
                    safe_roots=[temp_dir],
                    is_enabled=True,
                    metadata={"provider": "nas", "executor": "media_library_report"},
                )
                request = self.service.submit_request(
                    tool_name="media_library_report",
                    requested_inputs={"limit": 10},
                )
                buffer = StringIO()

                call_command("manage_tool_registry", execute=request.id, stdout=buffer)

            request.refresh_from_db()
            self.assertEqual(request.status, ToolExecutionRequest.STATUS_EXECUTED)
            output = buffer.getvalue()
            self.assertIn("Executed tool request", output)
            self.assertIn('"candidate_count": 1', output)

    def test_execute_request_prints_media_library_cleanup_result(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with override_settings(MEDIA_ROOT=temp_dir, ODDESY_SAFE_ROOTS=[temp_dir]):
                telegram_user = TelegramUser.objects.create(
                    telegram_user_id=556,
                    username="cleanup",
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
                    audit_requirements=["record operator id"],
                    safe_roots=[temp_dir],
                    is_enabled=True,
                    is_destructive=True,
                    metadata={"provider": "nas", "executor": "media_library_cleanup"},
                )
                request = self.service.submit_request(
                    tool_name="media_library_cleanup",
                    requested_inputs={"limit": 10},
                )
                call_command("manage_tool_registry", confirm=request.id)
                buffer = StringIO()

                call_command("manage_tool_registry", execute=request.id, stdout=buffer)

            request.refresh_from_db()
            self.assertEqual(request.status, ToolExecutionRequest.STATUS_EXECUTED)
            output = buffer.getvalue()
            self.assertIn("Executed tool request", output)
            self.assertIn('"deleted_asset_ids": [', output)
