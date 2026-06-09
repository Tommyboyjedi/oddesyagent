from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, override_settings

from apps.core.services.safe_root_browser import SafeRootBrowserError, SafeRootBrowserService


class SafeRootBrowserServiceTests(SimpleTestCase):
    def test_browse_lists_entries_within_safe_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "nested").mkdir()
            (root / "clip.mp4").write_bytes(b"video")
            service = SafeRootBrowserService()

            with override_settings(ODDESY_SAFE_ROOTS=[str(root)]):
                payload = service.browse(target_path=str(root), limit=10)

            self.assertEqual(payload["target_path"], str(root.resolve()))
            self.assertEqual(payload["count"], 2)
            names = {entry["name"] for entry in payload["entries"]}
            self.assertIn("nested", names)
            self.assertIn("clip.mp4", names)

    def test_browse_rejects_path_outside_safe_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            other = root.parent
            service = SafeRootBrowserService()

            with override_settings(ODDESY_SAFE_ROOTS=[str(root)]):
                with self.assertRaises(SafeRootBrowserError):
                    service.browse(target_path=str(other))

    def test_browse_requires_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            file_path = root / "clip.mp4"
            file_path.write_bytes(b"video")
            service = SafeRootBrowserService()

            with override_settings(ODDESY_SAFE_ROOTS=[str(root)]):
                with self.assertRaises(SafeRootBrowserError):
                    service.browse(target_path=str(file_path))
