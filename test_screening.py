"""store_findings screening gate: optional, fail-open, withhold before write."""
import json
import os
import uuid
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from allauth.account.models import EmailAddress

from clipshelf import judgment, models, services, interpretation
from clipshelf.interpretation import GuardrailBlocked

PASSWORD = "correct horse battery staple 42!"


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


# ------------------------------------------------------------- key plumbing

class ScreeningKeyTests(TestCase):
    def test_row_key_beats_env(self):
        s = services.get_settings()
        s.typesafe_api_key = "row-key"
        s.save(update_fields=["typesafe_api_key"])
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "env-key"}):
            self.assertEqual(services.screening_key(), "row-key")

    def test_env_fallback_when_row_blank(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "env-key"}):
            self.assertEqual(services.screening_key(), "env-key")

    def test_none_when_unset(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            self.assertIsNone(services.screening_key())


class AvailableKeyTests(SimpleTestCase):
    def test_explicit_key_without_env(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            self.assertTrue(judgment.available(" row-key "))
            self.assertFalse(judgment.available("   "))
            self.assertFalse(judgment.available(None))

    def test_env_fallback_intact(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "env-key"}):
            self.assertTrue(judgment.available(None))
            self.assertTrue(judgment.available("row-key"))


class JudgmentClientTests(SimpleTestCase):
    """judgment's own seam: key/model resolution and answer -> result mapping,
    with the TypeSafe client faked at its constructor."""

    def fake_client(self, answers):
        calls = []

        class Client:
            def __init__(self, **kwargs):
                calls.append(("init", kwargs))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def system_one(self, state, questions, timeout):
                calls.append(("ask", state, sorted(questions)))
                return SimpleNamespace(answers=answers)

        return calls, mock.patch.object(judgment, "TypeSafeClient", Client)

    def test_explicit_key_beats_env_and_env_names_the_model(self):
        calls, client = self.fake_client({"ai_tool": SimpleNamespace(noul=0.9)})
        env = {"TYPESAFE_API_KEY": "env-key", "TYPESAFE_MODEL": "model-x"}
        with mock.patch.dict(os.environ, env), client:
            self.assertEqual(judgment.ping(api_key=" row-key "), 0.9)
        self.assertEqual(calls[0][1]["api_key"], "row-key")
        self.assertEqual(calls[0][1]["model"], "model-x")

    def test_env_key_is_the_fallback_and_model_defaults_to_none(self):
        calls, client = self.fake_client({"ai_tool": SimpleNamespace(noul=0.4)})
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "env-key"}), client:
            os.environ.pop("TYPESAFE_MODEL", None)
            judgment.ping()
        self.assertEqual(calls[0][1]["api_key"], "env-key")
        self.assertIsNone(calls[0][1]["model"])

    def test_screen_material_maps_each_answer_to_its_verdict(self):
        calls, client = self.fake_client({
            "text_steer": SimpleNamespace(noul=0.1),
            "meta_steer": SimpleNamespace(noul=0.2),
            "severity": SimpleNamespace(score=1.5)})
        with client:
            result = judgment.screen_material({"url": "https://a.test/"}, api_key="k")
        self.assertEqual(result, {"text_steer": 0.1, "meta_steer": 0.2, "severity": 1.5})
        self.assertEqual(calls[1][2], ["meta_steer", "severity", "text_steer"])

    def test_screen_findings_maps_danger_and_per_entry_relatedness(self):
        entries = ["https://a.test/1", "https://b.test/2"]
        calls, client = self.fake_client({
            "danger": SimpleNamespace(score=0.5),
            "rel_0": SimpleNamespace(noul=0.9),
            "rel_1": SimpleNamespace(noul=0.1)})
        state = {"material": {"title": "T"}, "findings": {"summary": "s"},
                 "entries": entries}
        with client:
            result = judgment.screen_findings(state, api_key="k")
        self.assertEqual(result, {"danger": 0.5,
                                  "related": {entries[0]: 0.9, entries[1]: 0.1}})
        self.assertEqual(calls[1][1], state)

    def test_screen_findings_accepts_entries_max_and_rejects_one_more(self):
        n = judgment.ENTRIES_MAX
        answers = {"danger": SimpleNamespace(score=0.0)}
        answers.update({f"rel_{i}": SimpleNamespace(noul=1.0) for i in range(n)})
        _, client = self.fake_client(answers)
        entries = [f"https://e.test/{i}" for i in range(n + 1)]
        with client:
            result = judgment.screen_findings(
                {"material": {}, "findings": {}, "entries": entries[:n]}, api_key="k")
            self.assertEqual(len(result["related"]), n)
            with self.assertRaises(judgment.JudgmentError):
                judgment.screen_findings(
                    {"material": {}, "findings": {}, "entries": entries}, api_key="k")

    def test_missing_entry_answer_is_named_in_the_error(self):
        _, client = self.fake_client({"danger": SimpleNamespace(score=0.0),
                                      "rel_0": SimpleNamespace(noul=1.0)})
        state = {"material": {}, "findings": {},
                 "entries": ["https://a.test/1", "https://b.test/2"]}
        with client, self.assertRaisesMessage(
                judgment.JudgmentError, "non-numeric answer 'rel_1'"):
            judgment.screen_findings(state, api_key="k")


class InterpretKeyTests(SimpleTestCase):
    def test_interpret_forwards_screening_key_to_screen_material(self):
        seen = []

        def recorder(state, api_key=None):
            seen.append(api_key)
            # steering + severe: GuardrailBlocked fires before any LLM call
            return {"text_steer": 0.9, "meta_steer": 0.0, "severity": 2.0}

        source = {"url": "https://example.com/a", "text": "hello"}
        config = {"base_url": "http://127.0.0.1:9/v1", "model": "m", "api_key": "llm-key"}
        with mock.patch.object(judgment, "available", return_value=True), \
                mock.patch.object(judgment, "screen_material", side_effect=recorder):
            with self.assertRaises(GuardrailBlocked):
                interpretation.interpret(source, config, [], screening_key="row-key")
        self.assertEqual(seen, ["row-key"])


class StoreFindingsKeyTests(ScreeningMixin, TestCase):
    def test_gate_screens_with_settings_row_key(self):
        s = services.get_settings()
        s.typesafe_api_key = "row-key"
        s.save(update_fields=["typesafe_api_key"])
        job = self.make_job()
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), \
                mock.patch.object(services.judgment, "screen_findings",
                                  return_value={"danger": 0.1, "related": {}}) as screen:
            self.assertTrue(services.store_findings(
                job, self.findings(job, ["https://k.test/x"])))
        self.assertEqual(screen.call_args.kwargs["api_key"], "row-key")


# -------------------------------------------------------------- admin API

class ScreeningApiTests(TestCase):
    def setUp(self):
        self.admin = models.User.objects.create_user(
            username="admin1", email="admin1@example.com", password=PASSWORD,
            is_app_admin=True)
        EmailAddress.objects.create(user=self.admin, email=self.admin.email,
                                    primary=True, verified=True)
        self.client.force_login(self.admin)

    def admin_post(self, url, body, *, reauth=False):
        payload = dict(body)
        if reauth:
            payload["reauth_password"] = PASSWORD
        return self.client.post(url, data=json.dumps(payload),
                                content_type="application/json")

    def set_row_key(self, value):
        s = services.get_settings()
        s.typesafe_api_key = value
        s.save(update_fields=["typesafe_api_key"])
        return s

    def test_post_saves_key_only_with_reauth(self):
        self.assertEqual(
            self.admin_post("/api/admin/screening", {"api_key": "sekrit"}).status_code, 403)
        self.assertEqual(services.get_settings().typesafe_api_key, "")
        response = self.admin_post("/api/admin/screening", {"api_key": "sekrit"}, reauth=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(services.get_settings().typesafe_api_key, "sekrit")

    def test_post_clears_key_and_rejects_missing_api_key(self):
        self.set_row_key("sekrit")
        response = self.admin_post("/api/admin/screening", {"api_key": ""}, reauth=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(services.get_settings().typesafe_api_key, "")
        response = self.admin_post("/api/admin/screening", {}, reauth=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "api_key is required")

    def test_get_never_echoes_the_key(self):
        self.set_row_key("sekrit")
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            response = self.client.get("/api/admin/screening")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("sekrit", response.content.decode())
        self.assertEqual(response.json(), {"screening": {"has_api_key": True,
                                                         "env_fallback": False}})

    def test_get_reports_environment_fallback(self):
        """The admin field says where the key comes from; an env-keyed server
        must not read as unguarded."""
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "env-key"}):
            response = self.client.get("/api/admin/screening")
        self.assertEqual(response.json(), {"screening": {"has_api_key": False,
                                                         "env_fallback": True}})

    def test_check_requires_reauth(self):
        self.assertEqual(self.admin_post("/api/admin/screening/check", {}).status_code, 403)

    def test_check_unavailable_without_any_key(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            response = self.admin_post("/api/admin/screening/check", {}, reauth=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {"ok": False, "message": "screening unavailable: no API key set"})

    def test_check_ok_with_stored_key(self):
        self.set_row_key("sekrit")
        with mock.patch.object(judgment, "available", return_value=True), \
                mock.patch.object(judgment, "ask",
                                  return_value={"ai_tool": mock.Mock(noul=0.98)}) as ask:
            response = self.admin_post("/api/admin/screening/check", {}, reauth=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "message": "screening ok (p=0.98)"})
        self.assertEqual(ask.call_args.kwargs["api_key"], "sekrit")

    def test_check_uses_typed_key_without_changing_stored_key(self):
        self.set_row_key("stored-key")
        with mock.patch.object(judgment, "available", return_value=True), \
                mock.patch.object(judgment, "ask",
                                  return_value={"ai_tool": mock.Mock(noul=0.98)}) as ask:
            response = self.admin_post(
                "/api/admin/screening/check", {"api_key": "typed-key"}, reauth=True
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(ask.call_args.kwargs["api_key"], "typed-key")
        self.assertEqual(services.get_settings().typesafe_api_key, "stored-key")

    def test_check_redacts_key_from_error_message(self):
        self.set_row_key("sekrit")
        with mock.patch.object(judgment, "available", return_value=True), \
                mock.patch.object(judgment, "ask",
                                  side_effect=judgment.JudgmentError(
                                      "request failed with sekrit inside")):
            response = self.admin_post("/api/admin/screening/check", {}, reauth=True)
        message = response.json()["message"]
        self.assertFalse(response.json()["ok"])
        self.assertNotIn("sekrit", message)
        self.assertIn("***", message)
