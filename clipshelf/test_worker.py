"""Self-check: add_extracted reports only real changes, not no-op merges."""
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core import management
from django.test import TestCase


class AddExtractedMergeTests(TestCase):
    def test_second_identical_run_reports_zero_changes(self):
        findings = [{
            "repos": ["owner/repo"],
            "prompts": [{"text": "use uv", "title": "uv", "cat": ""}],
        }]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "findings.json"
            path.write_text(json.dumps(findings), encoding="utf-8")
            user = get_user_model().objects.create_user(
                username="merger", email="merge@clipshelf.test")

            first = StringIO()
            management.call_command(
                "add_extracted", "--user", user.email, str(path), stdout=first)
            second = StringIO()
            management.call_command(
                "add_extracted", "--user", user.email, str(path), stdout=second)

        self.assertIn("1 link change(s), 1 prompt change(s)", first.getvalue())
        self.assertIn("0 link change(s), 0 prompt change(s), 0 updated",
                      second.getvalue())
