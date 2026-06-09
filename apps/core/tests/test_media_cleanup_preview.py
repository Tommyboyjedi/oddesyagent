import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, override_settings

from apps.core.services.media_cleanup_preview import MediaCleanupPreviewService


class MediaCleanupPreviewServiceTests(SimpleTestCase):
    def test_preview_filters_by_extension_and_age(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_video = root / "old.mp4"
            recent_image = root / "recent.png"
            ignored_text = root / "notes.txt"
            old_video.write_bytes(b"video")
            recent_image.write_bytes(b"image")
            ignored_text.write_text("ignore", encoding="utf-8")

            old_timestamp = time.time() - (2 * 24 * 60 * 60)
            os.utime(old_video, (old_timestamp, old_timestamp))

            service = MediaCleanupPreviewService()
            with override_settings(ODDESY_SAFE_ROOTS=[str(root)]):
                payload = service.preview(
                    target_path=str(root),
                    older_than_days=1,
                    extensions=[".mp4", ".png"],
                    limit=10,
                )

            self.assertEqual(payload["candidate_count"], 1)
            self.assertEqual(payload["total_size_bytes"], len(b"video"))
            self.assertEqual(payload["candidates"][0]["name"], "old.mp4")

    def test_preview_uses_default_extensions(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "clip.webm").write_bytes(b"video")
            service = MediaCleanupPreviewService()

            with override_settings(ODDESY_SAFE_ROOTS=[str(root)]):
                payload = service.preview(target_path=str(root), limit=10)

            self.assertEqual(payload["candidate_count"], 1)
            self.assertEqual(payload["candidates"][0]["name"], "clip.webm")
