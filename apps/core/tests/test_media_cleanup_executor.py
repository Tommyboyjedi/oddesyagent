import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, override_settings

from apps.core.services.media_cleanup_executor import MediaCleanupExecutorService


class MediaCleanupExecutorServiceTests(SimpleTestCase):
    def test_execute_deletes_matching_candidates_and_reports_freed_bytes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_video = root / "old.mp4"
            keep_text = root / "notes.txt"
            old_video.write_bytes(b"video")
            keep_text.write_text("keep", encoding="utf-8")

            old_timestamp = time.time() - (2 * 24 * 60 * 60)
            os.utime(old_video, (old_timestamp, old_timestamp))

            service = MediaCleanupExecutorService()
            with override_settings(ODDESY_SAFE_ROOTS=[str(root)]):
                payload = service.execute(
                    target_path=str(root),
                    older_than_days=1,
                    extensions=[".mp4"],
                    limit=10,
                )

            self.assertEqual(payload["candidate_count"], 1)
            self.assertEqual(payload["deleted_count"], 1)
            self.assertEqual(payload["freed_bytes"], len(b"video"))
            self.assertEqual(payload["deleted_paths"], [str(old_video.resolve())])
            self.assertFalse(old_video.exists())
            self.assertTrue(keep_text.exists())

    def test_execute_skips_missing_candidate_after_preview(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_video = root / "old.mp4"
            old_video.write_bytes(b"video")
            old_timestamp = time.time() - (2 * 24 * 60 * 60)
            os.utime(old_video, (old_timestamp, old_timestamp))

            service = MediaCleanupExecutorService()
            with override_settings(ODDESY_SAFE_ROOTS=[str(root)]):
                preview = service.preview_service.preview(
                    target_path=str(root),
                    older_than_days=1,
                    extensions=[".mp4"],
                    limit=10,
                )
                old_video.unlink()
                payload = service.execute(
                    target_path=str(root),
                    older_than_days=1,
                    extensions=[".mp4"],
                    limit=10,
                )

            self.assertEqual(preview["candidate_count"], 1)
            self.assertEqual(payload["candidate_count"], 0)
            self.assertEqual(payload["deleted_count"], 0)
