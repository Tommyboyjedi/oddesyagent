import json

from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings

from apps.core.models import AuditLog, GenerationJob, MediaAsset, TelegramUser


@override_settings(
    ODDESY_INTERNAL_API_ENABLED=True,
    ODDESY_INTERNAL_API_TOKEN="secret-token",
)
class InternalApiTests(TestCase):
    def setUp(self) -> None:
        self.client = Client()
        self.auth_headers = {
            "HTTP_AUTHORIZATION": "Bearer secret-token",
            "REMOTE_ADDR": "127.0.0.1",
        }
        self.telegram_user = TelegramUser.objects.create(
            telegram_user_id=12345,
            username="tester",
            is_allowed=True,
        )
        self.input_media = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_INCOMING_IMAGE,
            original_file_name="input.jpg",
            telegram_file_id="telegram-file-1",
            file=ContentFile(b"image-bytes", name="input.jpg"),
        )
        self.output_media = MediaAsset.objects.create(
            telegram_user=self.telegram_user,
            asset_type=MediaAsset.TYPE_GENERATED_VIDEO,
            original_file_name="output.mp4",
            file=ContentFile(b"video-bytes", name="output.mp4"),
        )
        self.job = GenerationJob.objects.create(
            telegram_user=self.telegram_user,
            input_media=self.input_media,
            output_media=self.output_media,
            workflow_name="workflow_a",
            prompt="make video",
            seed=42,
        )

    @override_settings(ODDESY_INTERNAL_API_ENABLED=False)
    def test_internal_api_disabled_by_default(self) -> None:
        response = self.client.get("/api/internal/workflows/", **self.auth_headers)

        self.assertEqual(response.status_code, 404)

    def test_internal_api_rejects_missing_token(self) -> None:
        response = self.client.get("/api/internal/workflows/", REMOTE_ADDR="127.0.0.1")

        self.assertEqual(response.status_code, 401)

    def test_internal_api_rejects_non_loopback(self) -> None:
        response = self.client.get(
            "/api/internal/workflows/",
            HTTP_AUTHORIZATION="Bearer secret-token",
            REMOTE_ADDR="192.168.1.10",
        )

        self.assertEqual(response.status_code, 403)

    def test_list_workflows_endpoint(self) -> None:
        response = self.client.get("/api/internal/workflows/", **self.auth_headers)

        self.assertEqual(response.status_code, 200)
        self.assertIn("workflows", response.json())

    def test_create_job_from_existing_media_endpoint(self) -> None:
        response = self.client.post(
            "/api/internal/jobs/",
            data=json.dumps(
                {
                    "telegram_user_id": self.telegram_user.telegram_user_id,
                    "input_media_id": self.input_media.id,
                    "workflow_name": "i2v_wan_480p",
                    "prompt": "api prompt",
                    "seed": 99,
                    "metadata": {"source": "internal_api"},
                }
            ),
            content_type="application/json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["workflow_name"], "i2v_wan_480p")
        self.assertEqual(payload["seed"], 99)
        self.assertEqual(payload["metadata"]["source"], "internal_api")
        audit_log = AuditLog.objects.get(generation_job_id=payload["id"])
        self.assertEqual(audit_log.event_type, "job_created")
        self.assertEqual(audit_log.message, "queued")
        self.assertEqual(audit_log.metadata["source"], "internal_api")

    def test_job_status_endpoint(self) -> None:
        response = self.client.get(
            f"/api/internal/jobs/{self.job.id}/",
            data={"telegram_user_id": self.telegram_user.telegram_user_id},
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], self.job.id)

    def test_media_list_endpoint(self) -> None:
        response = self.client.get(
            "/api/internal/media/",
            data={
                "telegram_user_id": self.telegram_user.telegram_user_id,
                "asset_type": MediaAsset.TYPE_GENERATED_VIDEO,
            },
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()["media"]
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["id"], self.output_media.id)

    def test_generated_output_endpoint(self) -> None:
        response = self.client.get(
            f"/api/internal/jobs/{self.job.id}/output/",
            data={"telegram_user_id": self.telegram_user.telegram_user_id},
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], self.output_media.id)

    def test_internal_api_rejects_unknown_workflow(self) -> None:
        create_response = self.client.post(
            "/api/internal/jobs/",
            data=json.dumps(
                {
                    "telegram_user_id": self.telegram_user.telegram_user_id,
                    "input_media_id": self.input_media.id,
                    "workflow_name": "workflow_z",
                    "prompt": "boundary flow prompt",
                    "seed": 123,
                }
            ),
            content_type="application/json",
            **self.auth_headers,
        )
        self.assertEqual(create_response.status_code, 400)
        self.assertIn("Workflow not found", create_response.json()["detail"])

    def test_internal_api_rejects_invalid_json_body(self) -> None:
        response = self.client.post(
            "/api/internal/jobs/",
            data="{not-json}",
            content_type="application/json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Request body must be valid JSON.")

    def test_internal_api_rejects_non_integer_ids(self) -> None:
        response = self.client.post(
            "/api/internal/jobs/",
            data=json.dumps(
                {
                    "telegram_user_id": "abc",
                    "input_media_id": self.input_media.id,
                    "workflow_name": "i2v_wan_480p",
                }
            ),
            content_type="application/json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Field 'telegram_user_id' must be an integer.")

    def test_internal_api_output_endpoint_returns_404_for_cleaned_output(self) -> None:
        self.output_media.metadata["cleanup"] = {"removed_from_library": True}
        self.output_media.save(update_fields=["metadata"])
        self.output_media.file.delete(save=False)

        response = self.client.get(
            f"/api/internal/jobs/{self.job.id}/output/",
            data={"telegram_user_id": self.telegram_user.telegram_user_id},
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 404)
