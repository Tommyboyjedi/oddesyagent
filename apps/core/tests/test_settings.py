from django.test import SimpleTestCase

from pathlib import Path
from tempfile import TemporaryDirectory

from oddesyagent.settings import parse_allowed_user_ids, resolve_workflows_dir


class SettingsHelpersTests(SimpleTestCase):
    def test_parse_allowed_user_ids(self) -> None:
        self.assertEqual(parse_allowed_user_ids("123, 456 ,789"), [123, 456, 789])
        self.assertEqual(parse_allowed_user_ids(""), [])

    def test_resolve_workflows_dir_prefers_explicit_env_value(self) -> None:
        base_dir = Path(r"C:\repo")

        resolved = resolve_workflows_dir(
            base_dir,
            env_value=r"C:\custom\workflows",
            candidate_paths=[r"C:\ignored", base_dir / "workflows"],
        )

        self.assertEqual(resolved, Path(r"C:\custom\workflows").resolve())

    def test_resolve_workflows_dir_falls_back_to_repo_workflows(self) -> None:
        base_dir = Path(r"C:\repo")

        resolved = resolve_workflows_dir(
            base_dir,
            candidate_paths=[
                Path(r"C:\missing-one"),
                Path(r"C:\missing-two"),
                base_dir / "workflows",
            ],
        )

        self.assertEqual(resolved, (base_dir / "workflows").resolve())

    def test_resolve_workflows_dir_uses_first_existing_candidate(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()

            resolved = resolve_workflows_dir(
                root,
                candidate_paths=[first, second, root / "workflows"],
            )

            self.assertEqual(resolved, first.resolve())
