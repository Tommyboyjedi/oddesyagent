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

    def test_extract_outputs_from_prompt_history_flattens_outputs(self) -> None:
        client = ComfyUIClient(base_url="http://127.0.0.1:8188")

        outputs = client.extract_outputs_from_prompt_history(
            {
                "outputs": {
                    "12": {
                        "images": [
                            {"filename": "clip.mp4", "subfolder": "videos", "type": "output"},
                        ]
                    }
                }
            }
        )

        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["filename"], "clip.mp4")
        self.assertEqual(outputs[0]["node_id"], "12")
        self.assertEqual(outputs[0]["output_key"], "images")

    def test_extract_execution_error_from_prompt_history_reads_terminal_error(self) -> None:
        client = ComfyUIClient(base_url="http://127.0.0.1:8188")

        error = client.extract_execution_error_from_prompt_history(
            {
                "status": {
                    "status_str": "error",
                    "messages": [
                        [
                            "execution_error",
                            {
                                "node_id": "172",
                                "node_type": "SamplerCustomAdvanced",
                                "exception_message": "Sizes of tensors must match",
                            },
                        ]
                    ],
                }
            }
        )

        self.assertEqual(error, "Sizes of tensors must match (node 172:SamplerCustomAdvanced)")

    @patch("apps.core.services.comfyui_client.time.sleep")
    @patch("apps.core.services.comfyui_client.time.time")
    def test_wait_for_completion_waits_for_completed_status(self, mock_time: Mock, mock_sleep: Mock) -> None:
        client = ComfyUIClient(base_url="http://127.0.0.1:8188")
        client.get_history = Mock(
            side_effect=[
                {},
                {"abc123": {"outputs": {}, "status": {"completed": False, "status_str": "running"}}},
                {
                    "abc123": {
                        "outputs": {"12": {"images": [{"filename": "clip.mp4"}]}},
                        "status": {"completed": True, "status_str": "success"},
                    }
                },
            ]
        )
        mock_time.side_effect = [0, 1, 2, 3]

        result = client.wait_for_completion("abc123", poll_seconds=1, timeout_seconds=10)

        self.assertIn("12", result["outputs"])
        self.assertEqual(client.get_history.call_count, 3)

    @patch("apps.core.services.comfyui_client.time.sleep")
    @patch("apps.core.services.comfyui_client.time.time")
    def test_wait_for_completion_returns_on_terminal_error_status(self, mock_time: Mock, mock_sleep: Mock) -> None:
        client = ComfyUIClient(base_url="http://127.0.0.1:8188")
        client.get_history = Mock(
            return_value={
                "abc123": {
                    "outputs": {},
                    "status": {
                        "completed": False,
                        "status_str": "error",
                        "messages": [
                            [
                                "execution_error",
                                {
                                    "node_id": "172",
                                    "node_type": "SamplerCustomAdvanced",
                                    "exception_message": "Sizes of tensors must match",
                                },
                            ]
                        ],
                    },
                }
            }
        )
        mock_time.side_effect = [0, 1]

        result = client.wait_for_completion("abc123", poll_seconds=1, timeout_seconds=1800)

        self.assertEqual(result["status"]["status_str"], "error")
        self.assertEqual(client.get_history.call_count, 1)
