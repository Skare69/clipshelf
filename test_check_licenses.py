import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_licenses

FAKE_INV = {
    "allow": {"ids": ["Apache-2.0"]},
    "android": {
        "non_packaged": ["com.example:bom-metadata"],
        "entries": {
            "com.example:dep": {"observed": "1.0", "license": "Apache-2.0"},
        },
    },
}


class CheckAndroidTests(unittest.TestCase):
    def run_android(self, deps_text, inv=FAKE_INV):
        with tempfile.TemporaryDirectory() as directory:
            deps_file = Path(directory) / "deps.txt"
            deps_file.write_text(deps_text, encoding="utf-8")
            errors = []
            with patch.object(check_licenses, "INV", inv), patch.object(
                    check_licenses, "errors", errors):
                check_licenses.check_android(str(deps_file))
        return errors

    def assert_version_changed(self, errors, key, resolved):
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(key, errors[0])
        self.assertIn(f"resolved {resolved}", errors[0])
        self.assertIn("reviewed 1.0", errors[0])

    def test_hyphenated_group_version_compared(self):
        inv = {
            "allow": FAKE_INV["allow"],
            "android": {"non_packaged": FAKE_INV["android"]["non_packaged"],
                        "entries": {"org.some-group:hyphen-art": {"observed": "1.0", "license": "Apache-2.0"}}},
        }
        errors = self.run_android(
            "+--- org.some-group:hyphen-art:9.9\n", inv=inv)
        self.assert_version_changed(errors, "org.some-group:hyphen-art", "9.9")

    def test_bom_without_initial_version_compared(self):
        errors = self.run_android(
            "+--- com.example:dep -> 9.9\n")
        self.assert_version_changed(errors, "com.example:dep", "9.9")

    def test_strictly_constraint_resolved_version_compared(self):
        errors = self.run_android(
            "+--- com.example:dep:{strictly 1.0} -> 9.9\n")
        self.assert_version_changed(errors, "com.example:dep", "9.9")

    def test_forced_version_marker_compared(self):
        errors = self.run_android(
            "+--- com.example:dep:9.9!!\n")
        self.assert_version_changed(errors, "com.example:dep", "9.9")

    def test_version_range_resolved_version_compared(self):
        errors = self.run_android(
            "+--- com.example:dep:[1.0,2.0) -> 9.9\n")
        self.assert_version_changed(errors, "com.example:dep", "9.9")

    def test_unlisted_module_reported(self):
        errors = self.run_android(
            "+--- com.example:dep:1.0\n"
            "+--- com.example:stranger:1.0\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("com.example:stranger:1.0 is not in", errors[0])

    def test_valid_and_excluded_lines_pass(self):
        errors = self.run_android(
            "+--- com.example:dep:1.0\n"
            "+--- com.example:constraint:1.0 (c)\n"
            "+--- com.example:dep:1.0 (*)\n"
            "+--- com.example:bom-metadata:1.0\n")
        self.assertEqual(errors, [])

    def test_inventoried_but_unresolved_reported(self):
        inv = {
            "allow": FAKE_INV["allow"],
            "android": {
                "non_packaged": FAKE_INV["android"]["non_packaged"],
                "entries": {
                    **FAKE_INV["android"]["entries"],
                    "com.example:gone": {"observed": "1.0", "license": "Apache-2.0"},
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            deps_file = Path(directory) / "deps.txt"
            deps_file.write_text("+--- com.example:dep:1.0\n", encoding="utf-8")
            errors = []
            with patch.object(check_licenses, "INV", inv), patch.object(
                    check_licenses, "errors", errors):
                check_licenses.check_android(str(deps_file))
        self.assertEqual(errors, [
            "android: com.example:gone is inventoried but not resolved"])

    def test_malformed_dependency_node_fails_closed(self):
        errors = self.run_android(
            "+--- com.example\n"
            "+--- com.example:dep:1.0\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("unparseable dependency node", errors[0])
        self.assertIn("'com.example'", errors[0])

    def test_missing_separator_fails_closed(self):
        errors = self.run_android(
            "+--- com.example:dep:1.0\n"
            "+---+---com.example:dep:1.0\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("unparseable dependency node", errors[0])
        self.assertIn("com.example:dep:1.0", errors[0])

    def test_empty_group_fails_closed(self):
        errors = self.run_android(
            "+--- com.example:dep:1.0\n"
            "+--- :dep:1.0\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("unparseable dependency node", errors[0])

    def test_empty_artifact_fails_closed(self):
        errors = self.run_android(
            "+--- com.example:dep:1.0\n"
            "+--- g::1.0\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("unparseable dependency node", errors[0])

    def test_arrow_without_initial_version_fails_closed(self):
        errors = self.run_android(
            "+--- com.example:dep:1.0\n"
            "+--- g:a: -> 1.0\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("unparseable dependency node", errors[0])

    def test_malformed_constraint_line_fails_closed(self):
        errors = self.run_android(
            "+--- com.example:dep:1.0\n"
            "+--- rubbish (c)\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("unparseable", errors[0])

    def test_resolved_forced_pin_passes(self):
        errors = self.run_android(
            "+--- com.example:dep:1.0!! (*)\n")
        self.assertEqual(errors, [])

    def test_constraint_marker_embedded_in_version_fails_closed(self):
        errors = self.run_android(
            "+--- com.example:dep:1.0\n"
            "+--- g:a:1(c)0\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("unparseable", errors[0])


class CheckImageTests(unittest.TestCase):
    def test_checks_runtime_external_base_and_ignores_stage_alias(self):
        image = check_licenses.INV["image"]
        sqlite = image["sqlite"]
        dockerfile = (
            f"FROM {image['base']} AS builder\n"
            "RUN apt-get install -y --no-install-recommends ca-certificates gcc libc6-dev make \\\n"
            "    && true\n"
            "FROM builder AS inherited\n"
            "FROM python:3.13-alpine AS runtime\n"
            f"ARG SQLITE_VERSION={sqlite['version']}\n"
            "RUN apt-get install -y --no-install-recommends ca-certificates ffmpeg \\\n"
            "    && true\n"
            f"ARG SQLITE_TARBALL={sqlite['tarball']}\n"
            f"ARG SQLITE_URL={sqlite['url']}\n"
            f"ARG SQLITE_SHA256={sqlite['sha256']}\n"
            "COPY THIRD_PARTY_NOTICES.md THIRD_PARTY_NOTICES.md\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            errors = []
            with patch.object(check_licenses, "ROOT", root), patch.object(
                    check_licenses, "errors", errors):
                check_licenses.check_image()

        self.assertEqual(
            errors,
            [f"image: base image is python:3.13-alpine, inventory pins {image['base']}"],
        )


if __name__ == "__main__":
    unittest.main()
