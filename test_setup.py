"""First-run setup wizard: an empty database funnels every request to /setup,
the first visitor becomes the verified admin, and the wizard closes afterwards.

Real database, real URLconf, real middleware — no mocks.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from allauth.account.models import EmailAddress

from clipshelf import setup

User = get_user_model()
PASSWORD = "first-run-passphrase-9"


class SetupWizardTests(TestCase):
    def setUp(self):
        # needs_setup() latches "done" in a module-level flag once any user
        # row exists; other test modules create users before this one runs, so
        # reset the latch to exercise the empty-database state. Restoring it
        # afterwards keeps later modules (which all run with users present)
        # unaffected.
        self._latched = setup._setup_done
        setup._setup_done = False
        self.addCleanup(setattr, setup, "_setup_done", self._latched)

    def _signup(self, client, email="owner@example.com", password1=PASSWORD,
                password2=None):
        return client.post("/setup", data={
            "email": email,
            "password1": password1,
            "password2": password1 if password2 is None else password2,
        })

    def test_empty_database_redirects_to_setup_and_healthz_stays_up(self):
        client = Client()
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/setup")
        # The healthcheck must answer before setup so orchestrators can wait
        # for a fresh container.
        self.assertEqual(client.get("/healthz").status_code, 200)

    def test_first_visitor_becomes_verified_admin_and_is_logged_in(self):
        client = Client()
        response = self._signup(client)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")
        self.assertEqual(User.objects.count(), 1)
        user = User.objects.get()
        self.assertTrue(user.is_app_admin)
        self.assertTrue(user.check_password(PASSWORD))
        # api_user rejects unverified mailboxes: an unverified admin would be
        # locked out of the API it just bootstrapped.
        address = EmailAddress.objects.get(user=user, primary=True)
        self.assertTrue(address.verified)
        self.assertEqual(address.email, "owner@example.com")
        self.assertEqual(client.get("/").status_code, 200)

    def test_after_setup_wizard_is_closed(self):
        self._signup(Client())
        client = Client()
        response = client.get("/setup")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], settings.LOGIN_URL)
        self._signup(client, email="second@example.com")
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(User.objects.filter(email="second@example.com").exists())

    def test_mismatched_confirmation_renders_error_without_user(self):
        response = self._signup(Client(), password2="different-passphrase-9")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Passwords do not match.")
        self.assertEqual(User.objects.count(), 0)

    def test_weak_password_renders_error_without_user(self):
        response = self._signup(Client(), password1="short")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "too short")
        self.assertEqual(User.objects.count(), 0)
