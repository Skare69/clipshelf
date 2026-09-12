"""Worker coordinator regressions: claims, recovery, pauses, finite failures."""
import shutil
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from clipshelf import models, services, worker

FINDINGS = {"source": "", "summary": "good findings", "repos": [], "prompts": [],
            "links": [], "categories": [], "installs": [], "warnings": []}


def fake_acquire(url, directory, status="complete"):
    path = Path(directory) / "page.html"
    path.write_text(f"<html>{url}</html>", encoding="utf-8")
    return {"url": url, "original_url": url, "title": "t", "desc": "d", "text": "x",
            "links": [], "assets": [{"path": str(path), "kind": "page",
                                     "content_type": "text/html", "position": 0}],
            "acquisition": status, "warnings": [], "metadata": {}}


class WorkerMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clipshelf-test-"))
        self.user = models.User.objects.create_user(username="w1", email="w1@example.com")
        self.personal = services.personal_collection(self.user)
        cfg = services.get_settings()
        cfg.llm_base_url = "http://llm.test"
        cfg.llm_model = "test-model"
        cfg.save()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_job(self, url="https://example.com/a", collection=None, **fields):
        capture = models.Capture.objects.create(
            user=self.user, client_request_id=str(uuid.uuid4()), raw_text=url,
            requested_collection_id=None, collection=collection or self.personal,
            request_hash="h", received_at=timezone.now(), notice="")
        defaults = dict(state="queued", acquisition="pending", interpretation="pending",
                        attempts=0, collection=collection or self.personal)
        defaults.update(fields)
        return models.Job.objects.create(capture=capture, url=url, **defaults)

    def run_worker(self, interpret=None, acquire=fake_acquire):
        side = interpret or (lambda source, config, cats: {**FINDINGS, "source": source.get("url")})
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire", side_effect=acquire), \
                mock.patch("clipshelf.interpretation.interpret", side_effect=side):
            worker.main(once=True)

    def refresh(self, job):
        job.refresh_from_db()
        return job


class WorkerPipelineTests(WorkerMixin, TransactionTestCase):
    def test_pipeline_publishes_assets_before_done(self):
        job = self.make_job()
        self.run_worker()
        self.refresh(job)
        self.assertEqual(job.state, "done")
        self.assertEqual(job.acquisition, "complete")
        self.assertEqual(job.interpretation, "complete")
        self.assertEqual(job.findings["summary"], "good findings")
        asset = models.Asset.objects.get(job=job)
        published = Path(self.tmp) / asset.path
        self.assertTrue(published.is_file(), "no complete asset file at final path")
        self.assertTrue(str(asset.path).startswith("assets/"))

    def test_abandoned_running_recovered(self):
        job = self.make_job(state="running")
        self.run_worker()
        self.assertEqual(self.refresh(job).state, "done")

    def test_disabled_user_pauses_without_processing(self):
        job = self.make_job()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.run_worker()
        self.refresh(job)
        self.assertEqual(job.state, "queued")
        self.assertEqual(job.acquisition, "pending")
        self.assertFalse(models.Asset.objects.filter(job=job).exists())
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        self.run_worker()
        self.assertEqual(self.refresh(job).state, "done")

    def test_revoked_rights_pause(self):
        other = models.User.objects.create_user(username="w2", email="w2@example.com")
        shared = models.Collection.objects.create(name="s", kind="shared", owner=other)
        models.Membership.objects.create(collection=shared, user=self.user)
        job = self.make_job(collection=shared)
        models.Membership.objects.filter(collection=shared, user=self.user).delete()
        self.run_worker()
        self.refresh(job)
        self.assertEqual(job.state, "queued")
        self.assertTrue(job.error.startswith("paused:"))

    def test_missing_config_blocks_without_burning_attempts(self):
        cfg = services.get_settings()
        cfg.llm_base_url = ""
        cfg.save()
        job = self.make_job(acquisition="complete", source={}, warnings=[])
        self.run_worker()
        self.refresh(job)
        self.assertEqual(job.state, "blocked")
        self.assertEqual(job.error, worker.CONFIG_ERROR)
        self.assertEqual(job.attempts, 0)
        cfg.llm_base_url = "http://llm.test"
        cfg.save()
        self.run_worker()
        self.assertEqual(self.refresh(job).state, "done")

    def test_interpretation_failure_preserves_last_good_findings(self):
        job = self.make_job()
        self.run_worker()
        self.assertEqual(self.refresh(job).findings["summary"], "good findings")
        job.state = "queued"  # reprocess: pipeline must keep the last good findings
        job.interpretation = "pending"
        job.save()

        def boom(source, config, cats):
            from clipshelf import interpretation
            raise interpretation.InterpretationError("malformed output")

        self.run_worker(interpret=boom)
        self.refresh(job)
        self.assertEqual(job.state, "retry")
        self.assertEqual(job.attempts, 1)
        self.assertIn("malformed output", job.error)
        self.assertEqual(job.findings["summary"], "good findings")

    def test_finite_backoff_then_blocked(self):
        def boom(source, config, cats):
            from clipshelf import interpretation
            raise interpretation.InterpretationError("nope")

        job = self.make_job(acquisition="complete", source={})
        for attempt in range(worker.MAX_ATTEMPTS):
            if attempt:  # requeue: pull the backoff deadline into the past
                job.retry_at = timezone.now() - timezone.timedelta(seconds=1)
                job.save(update_fields=["retry_at"])
            self.run_worker(interpret=boom)
            self.refresh(job)
        self.assertEqual(job.attempts, worker.MAX_ATTEMPTS)
        self.assertEqual(job.state, "blocked")
        self.assertIsNone(job.retry_at)

    def test_claim_is_atomic(self):
        job = self.make_job()
        self.assertIsNotNone(worker._claim(job.id))
        self.assertIsNone(worker._claim(job.id))  # already running


def settings_path(rel):
    from django.conf import settings
    return Path(settings.DATA_DIR) / rel


class ImportQueueTests(WorkerMixin, TransactionTestCase):
    def test_pending_import_record_processed_idempotently(self):
        staging_rel = f"staging/import-{uuid.uuid4().hex}.json"
        items = [{"url": "https://example.com/imported", "title": "From browser"}]
        with override_settings(DATA_DIR=str(self.tmp)):
            (Path(self.tmp) / "staging").mkdir(parents=True, exist_ok=True)
            (Path(self.tmp) / staging_rel).write_text(_json(items), encoding="utf-8")

        def fake_import_export(batch, directory):
            out = []
            for item in batch:
                p = Path(directory) / "i.html"
                p.write_text("bytes", encoding="utf-8")
                out.append({"url": item["url"], "original_url": item["url"], "title": "",
                            "desc": "", "text": "", "links": [],
                            "assets": [{"path": str(p), "kind": "page",
                                        "content_type": "text/html", "position": 0}],
                            "acquisition": "complete", "warnings": [], "metadata": {}})
            return out

        record = models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"status": "pending", "staging": staging_rel,
                      "collection_id": str(self.personal.id),
                      "client_request_id": str(uuid.uuid4())})
        upstream = []

        def blocked_acquire(url, directory):
            upstream.append(url)
            return fake_acquire(url, directory, status="blocked")

        with mock.patch("clipshelf.acquisition.import_export", side_effect=fake_import_export):
            self.run_worker(acquire=blocked_acquire)
        record.refresh_from_db()
        self.assertEqual(record.manifest["status"], "done")
        self.assertEqual(record.manifest["result"]["counts"]["entries"], 1)
        self.assertTrue(models.Entry.objects.filter(
            collection=self.personal, kind="link",
            key="https://example.com/imported").exists())
        # Imported material is the acquisition: the worker interprets it and
        # never silently re-fetches the gated post from upstream.
        self.assertEqual(upstream, [])
        job = models.Job.objects.filter(url="https://example.com/imported").get()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.acquisition, "complete")
        self.assertEqual(job.interpretation, "complete")
        entry = models.Entry.objects.get(collection=self.personal, kind="link",
                                         key="https://example.com/imported")
        contributions = models.Contribution.objects.filter(entry=entry)
        self.assertEqual({c.user_id for c in contributions}, {self.user.id})
        self.assertEqual(
            sum(1 for c in contributions if (c.data or {}).get("findings")), 1)


class IngestCommandTests(WorkerMixin, TestCase):
    def test_ingest_idempotent_receipt(self):
        dump = self.tmp / "dump.txt"
        dump.write_text("read https://example.com/x later\n", encoding="utf-8")
        first = _ingest(dump)
        self.assertTrue(first["created"])
        second = _ingest(dump)
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"], "identical file must reuse the receipt")
        self.assertEqual(models.Capture.objects.filter(user=self.user).count(), 1)


def _json(obj):
    import json
    return json.dumps(obj)


def _ingest(path):
    import io
    import json as jsonlib
    out = io.StringIO()
    call_command("ingest", "--user", "w1@example.com", str(path), stdout=out)
    return jsonlib.loads(out.getvalue())
