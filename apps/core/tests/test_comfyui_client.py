from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.core.services.comfyui_client import ComfyUIClient


class ComfyUIClientTests(SimpleTestCase):
    @patch("apps.core.services.comfyui_client.requests.post")
    def test_submit_workflow_posts_expected_payload(self, mock_post: Mock) -> None:
        response = Mock()
        response.json.return_value = {"prompt_id": "abc123"}
        response.raise_for_status.return_value = None
        mock_post.return_value = response

        client = ComfyUIClient(base_url="http://127.0.0.1:8188")
        prompt_id = client.submit_workflow({"1": {"inputs": {}}})

        self.assertEqual(prompt_id, "abc123")
        mock_post.assert_called_once_with(
            "http://127.0.0.1:8188/prompt",
            json={"prompt": {"1": {"inputs": {}}}},
            timeout=30,
        )

    @patch("apps.core.services.comfyui_client.requests.get")
    def test_download_output_file_builds_expected_url(self, mock_get: Mock) -> None:
        response = Mock()
        response.content = b"video-bytes"
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        client = ComfyUIClient(base_url="http://127.0.0.1:8188")
        content = client.download_output_file("clip.mp4", subfolder="videos", output_type="output")

        self.assertEqual(content, b"video-bytes")
        mock_get.assert_called_once_with(
            "http://127.0.0.1:8188/view?filename=clip.mp4&subfolder=videos&type=output",
            timeout=30,
        )
