"""store_findings screening gate: optional, fail-open, withhold before write."""
import uuid
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from clipshelf import judgment, models, services
from clipshelf.interpretation import GuardrailBlocked


class ScreeningMixin:
    """Same DB fixtures as test_worker.WorkerMixin, without the data dir."""

    def setUp(self):
        self.user = models.User.objects.create_user(username="s1", email="s1@example.com")
        self.personal = services.personal_collection(self.user)

    def make_job(self, url="https://example.com/a"):
        capture = models.Capture.objects.create(
            user=self.user, client_request_id=str(uuid.uuid4()), raw_text=url,
            requested_collection_id=None, collection=self.personal,
            request_hash="h", received_at=timezone.now(), notice="")
        return models.Job.objects.create(
            capture=capture, url=url, collection=self.personal, state="queued",
            acquisition="pending", interpretation="pending", attempts=0)

    def findings(self, job, links=()):
        return {"source": job.url, "summary": "good findings", "repos": [], "prompts": [],
                "links": [{"url": u, "title": ""} for u in links], "categories": [],
                "installs": [], "warnings": []}


class ScreeningTests(ScreeningMixin, TestCase):
    def test_screening_off_publishes_unchanged(self):
        job = self.make_job()
        with mock.patch.object(services.judgment, "available", return_value=False):
            self.assertTrue(services.store_findings(job, self.findings(job, ["https://k.test/x"])))
        job.refresh_from_db()
        self.assertEqual(job.interpretation, "complete")
        self.assertEqual(job.findings["summary"], "good findings")
        self.assertEqual(len(job.findings["links"]), 1)
        self.assertEqual(job.findings["warnings"], [])

    def test_danger_withholds_before_any_write(self):
        job = self.make_job()
        with mock.patch.object(services.judgment, "available", return_value=True), \
                mock.patch.object(services.judgment, "screen_findings",
                                  return_value={"danger": 2.5, "related": {}}):
            with self.assertRaises(GuardrailBlocked):
                services.store_findings(job, self.findings(job, ["https://k.test/x"]))
        job.refresh_from_db()
        self.assertEqual(job.findings, {})
        self.assertEqual(job.state, "queued")
        self.assertEqual(job.interpretation, "pending")

    def test_unrelated_link_dropped_with_warning(self):
        job = self.make_job()
        planted = "https://planted.test/x"
        with mock.patch.object(services.judgment, "available", return_value=True), \
                mock.patch.object(services.judgment, "screen_findings",
                                  return_value={"danger": 0.1, "related": {planted: 0.01}}):
            self.assertTrue(services.store_findings(job, self.findings(
                job, [planted, "https://keep.test/y"])))
        job.refresh_from_db()
        self.assertEqual([e["url"] for e in job.findings["links"]], ["https://keep.test/y"])
        self.assertTrue(any("screening dropped 1 unrelated entry" in w
                            for w in job.findings["warnings"]))

    def test_related_link_kept(self):
        job = self.make_job()
        url = "https://k.test/x"
        with mock.patch.object(services.judgment, "available", return_value=True), \
                mock.patch.object(services.judgment, "screen_findings",
                                  return_value={"danger": 0.1, "related": {url: 0.47}}):
            self.assertTrue(services.store_findings(job, self.findings(job, [url])))
        job.refresh_from_db()
        self.assertEqual([e["url"] for e in job.findings["links"]], [url])

    def test_judgment_error_fails_open(self):
        job = self.make_job()
        with mock.patch.object(services.judgment, "available", return_value=True), \
                mock.patch.object(services.judgment, "screen_findings",
                                  side_effect=judgment.JudgmentError("boom")):
            self.assertTrue(services.store_findings(job, self.findings(job, ["https://k.test/x"])))
        job.refresh_from_db()
        self.assertEqual(job.interpretation, "complete")
        self.assertEqual(len(job.findings["links"]), 1)
        self.assertTrue(any("screening unavailable" in w for w in job.findings["warnings"]))
