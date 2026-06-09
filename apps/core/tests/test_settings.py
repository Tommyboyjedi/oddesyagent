from django.test import SimpleTestCase

from oddesyagent.settings import parse_allowed_user_ids


class SettingsHelpersTests(SimpleTestCase):
    def test_parse_allowed_user_ids(self) -> None:
        self.assertEqual(parse_allowed_user_ids("123, 456 ,789"), [123, 456, 789])
        self.assertEqual(parse_allowed_user_ids(""), [])
