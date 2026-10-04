import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_licenses


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
