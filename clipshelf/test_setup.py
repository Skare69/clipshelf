"""Setup wizard tests: per-field error linking (aria-describedby, aria-invalid).

The wizard is the product's first screen; every error must sit under the field
that caused it and be announced to that field's input, not to the Email input.
"""

import re

from django.test import TestCase

from clipshelf import setup as setup_module
from clipshelf.models import User

PASSWORD = "correct horse battery staple 42!"


def input_tag(html, input_id):
    """Return the single <input> tag with the given id from `html`."""
    for tag in re.findall(r"<input\b[^>]*>", html):
        if f'id="{input_id}"' in tag:
            return tag
    raise AssertionError(f"no <input> with id={input_id!r} in the response")


def error_block(html, block_id):
    """Return the inner HTML of the error <ul> with the given id."""
    match = re.search(r'<ul\b[^>]*id="%s"[^>]*>(.*?)</ul>' % block_id, html, re.S)
    if match is None:
        raise AssertionError(f"no error list with id={block_id!r} in the response")
    return match.group(1)


class SetupViewTests(TestCase):
    def setUp(self):
        super().setUp()
        # needs_setup() caches in a module flag once a user exists; each test
        # starts from the empty-database state and leaves no latch behind.
        setup_module._setup_done = False
        self.addCleanup(setattr, setup_module, "_setup_done", False)

    def test_mismatched_password_error_is_linked_to_password2(self):
        response = self.client.post(
            "/setup",
            {
                "email": "admin@example.com",
                "password1": PASSWORD,
                "password2": "a genuinely different passphrase",
            },
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        tag = input_tag(html, "id_password2")
        self.assertIn('aria-invalid="true"', tag)
        self.assertIn('aria-describedby="password2-errors"', tag)
        self.assertIn("Passwords do not match.", error_block(html, "password2-errors"))
        # The mismatch is not the Email field's problem and the old
        # single shared list is gone.
        self.assertNotIn("setup-errors", html)
        self.assertNotIn('aria-invalid="true"', input_tag(html, "id_email"))
        self.assertNotIn('aria-invalid="true"', input_tag(html, "id_password1"))

    def test_weak_password_error_is_linked_to_password1(self):
        response = self.client.post(
            "/setup",
            {"email": "admin@example.com", "password1": "short", "password2": "short"},
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        tag = input_tag(html, "id_password1")
        self.assertIn('aria-invalid="true"', tag)
        self.assertIn('aria-describedby="password1-errors"', tag)
        self.assertIn("too short", error_block(html, "password1-errors"))
        self.assertNotIn('aria-invalid="true"', input_tag(html, "id_password2"))

    def test_invalid_email_error_is_linked_to_email(self):
        response = self.client.post(
            "/setup",
            {"email": "not-an-email", "password1": PASSWORD, "password2": PASSWORD},
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        tag = input_tag(html, "id_email")
        self.assertIn('aria-invalid="true"', tag)
        self.assertIn('aria-describedby="email-errors"', tag)
        self.assertIn(
            "Enter a valid email address.", error_block(html, "email-errors")
        )
        self.assertNotIn('aria-invalid="true"', input_tag(html, "id_password1"))
        self.assertNotIn('aria-invalid="true"', input_tag(html, "id_password2"))

    def test_successful_setup_creates_admin_and_redirects(self):
        response = self.client.post(
            "/setup", {"email": "admin@example.com", "password1": PASSWORD, "password2": PASSWORD}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")
        self.assertTrue(
            User.objects.filter(email="admin@example.com", is_app_admin=True).exists()
        )
        self.assertIn("_auth_user_id", self.client.session)
        html = response.content.decode()
        self.assertNotIn('aria-invalid="true"', html)

    def test_get_renders_form_without_error_markup(self):
        response = self.client.get("/setup")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn("aria-invalid", html)
        self.assertNotIn("aria-describedby", html)
