import tempfile
import unittest
import zipfile
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


REAL = (Path(check_licenses.__file__).parent / "Dockerfile").read_text(encoding="utf-8")


def image_errors(dockerfile):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        errors = []
        with patch.object(check_licenses, "ROOT", root), patch.object(
                check_licenses, "errors", errors):
            check_licenses.check_image()
    return errors


def runtime_plus(snippet):
    """The shipped Dockerfile with `snippet` added to the runtime stage."""
    anchor = "WORKDIR /app\n"
    assert anchor in REAL
    return REAL.replace(anchor, snippet + "\n" + anchor)


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

        self.assertEqual(
            image_errors(dockerfile),
            [f"image: base image is python:3.13-alpine, inventory pins {image['base']}"],
        )

    def test_shipped_dockerfile_passes(self):
        self.assertEqual(image_errors(REAL), [])

    def test_any_apt_install_form_is_compared_against_inventory(self):
        for snippet in (
            "RUN apt-get update && apt-get install -y jq",
            "RUN apt install jq",
            "RUN apt-get -y install jq",
            "RUN DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get install -yq jq",
            "RUN apt-get update; apt-get -o Dpkg::Use-Pty=0 install jq || true",
            "RUN apt-get update \\\n# a comment line inside the continuation\n    && apt-get install jq",
        ):
            with self.subTest(snippet=snippet):
                errors = image_errors(runtime_plus(snippet))
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("apt stage sets", errors[0])
                self.assertIn("'jq'", errors[0])

    def test_unverifiable_package_manager_use_fails_closed(self):
        for snippet in (
            "RUN sh -c 'apt-get install -y jq'",
            "RUN <<EOF\nset -e\napt-get install -y jq\nEOF",
            "RUN python - <<'PY'\nimport os; os.system('apt-get install -y jq')\nPY",
            "RUN apt-get install -y $EXTRA",
            "RUN apt-get install -y jq=1.6-2.1",
            "RUN apt-get install -y ./vendor.deb",
            "RUN apt-get dist-upgrade -y",
            "RUN dpkg -i /tmp/vendor.deb",
            "RUN apk add jq",
            "RUN dnf install -y jq",
            "RUN microdnf install jq",
            "RUN yum install -y jq",
            "RUN rpm -i /tmp/vendor.rpm",
            "RUN zypper in jq",
            "RUN apt-get update # && apt-get install -y jq",
            "RUN echo 'unbalanced",
        ):
            with self.subTest(snippet=snippet):
                errors = image_errors(runtime_plus(snippet))
                self.assertTrue(any("cannot verify" in e for e in errors), errors)

    def test_external_image_sources_fail(self):
        for snippet in (
            "COPY --from=alpine:3.20 /bin/busybox /usr/local/bin/busybox",
            "COPY --chown=0:0 --from=busybox /bin/busybox /usr/local/bin/busybox",
            "RUN --mount=type=bind,from=alpine:3.20,target=/x cp /x/bin/busybox /usr/local/bin/",
            "RUN --mount=type=cache,from=${CACHE_IMAGE},target=/c true",
        ):
            with self.subTest(snippet=snippet):
                errors = image_errors(runtime_plus(snippet))
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("pulls external image", errors[0])

    def test_stage_and_pinned_base_sources_pass(self):
        base = check_licenses.INV["image"]["base"]
        for snippet in (
            "COPY --from=SQLITE-BUILD /usr/local/include /tmp/include",
            "COPY --from=0 /etc/os-release /tmp/os-release",
            f"COPY --from={base} /etc/os-release /tmp/os-release",
            "RUN --mount=type=bind,from=sqlite-build,target=/b true",
            "RUN rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/",
        ):
            with self.subTest(snippet=snippet):
                self.assertEqual(image_errors(runtime_plus(snippet)), [])


class CheckNoticesTests(unittest.TestCase):
    def run_notices(self, text):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apk = root / "android/app/src/main/assets/THIRD_PARTY_NOTICES.md"
            apk.parent.mkdir(parents=True)
            for path in (root / "THIRD_PARTY_NOTICES.md", apk):
                path.write_text(text, encoding="utf-8")
            errors = []
            with patch.object(check_licenses, "ROOT", root), patch.object(
                    check_licenses, "errors", errors):
                check_licenses.check_notices()
        return errors

    def test_removed_inventoried_rows_fail(self):
        text = (Path(check_licenses.__file__).parent
                / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        py = check_licenses.INV["python"]["entries"]["gallery-dl"]
        an = check_licenses.INV["android"]["entries"]["androidx.core:core"]
        rows = [
            f"| gallery-dl | {py['observed']} | **GPL-2.0-only** |",
            f"| androidx.core:core | {an['observed']} | Apache-2.0 |",
        ]
        for row in rows:
            self.assertIn(row, text)
            text = "\n".join(line for line in text.split("\n")
                             if not line.startswith(row))

        self.assertEqual(self.run_notices(text), [
            f"notices: python gallery-dl: inventory (version, license) "
            f"('{py['observed']}', 'GPL-2.0-only') != THIRD_PARTY_NOTICES.md row None",
            f"notices: android androidx.core:core: inventory (version, license) "
            f"('{an['observed']}', 'Apache-2.0') != THIRD_PARTY_NOTICES.md row None",
        ])


class CheckNoticesApkTests(unittest.TestCase):
    def run_notices(self, root):
        inv = {"python": {"entries": {}}, "android": {"entries": {}}}
        errors = []
        with patch.object(check_licenses, "ROOT", Path(root)), patch.object(
                check_licenses, "INV", inv), patch.object(
                check_licenses, "errors", errors):
            check_licenses.check_notices()
        return errors

    def test_missing_root_notices_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            errors = self.run_notices(directory)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("missing", errors[0])

    def test_present_root_notices_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "THIRD_PARTY_NOTICES.md").write_text("n", encoding="utf-8")
            self.assertEqual(self.run_notices(directory), [])

    def run_apk(self, path):
        errors = []
        with patch.object(check_licenses, "errors", errors):
            check_licenses.check_apk(str(path))
        return errors

    def test_apk_packing_notices_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "app.apk"
            with zipfile.ZipFile(apk, "w") as z:
                z.writestr("assets/THIRD_PARTY_NOTICES.md", "notices")
            self.assertEqual(self.run_apk(apk), [])

    def test_apk_without_notices_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "app.apk"
            with zipfile.ZipFile(apk, "w") as z:
                z.writestr("classes.dex", "dex")
            errors = self.run_apk(apk)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("assets/THIRD_PARTY_NOTICES.md", errors[0])

    def test_unreadable_apk_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "app.apk"
            apk.write_text("not a zip", encoding="utf-8")
            errors = self.run_apk(apk)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("cannot read", errors[0])

    def test_missing_apk_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            errors = self.run_apk(Path(directory) / "absent.apk")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("cannot read", errors[0])


if __name__ == "__main__":
    unittest.main()
