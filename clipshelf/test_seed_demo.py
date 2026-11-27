"""seed_demo: dev-only, empty-data-dir-only, and the seeded data covers the UI."""
import io
import re

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from clipshelf import services
from clipshelf.models import Collection, Entry, Job, User


@override_settings(DEBUG=True)
class SeedDemoTests(TestCase):
    def seed(self):
        out = io.StringIO()
        call_command("seed_demo", stdout=out)
        return out.getvalue()

    def test_seeds_library_inbox_and_login(self):
        password = re.search(r"password \(both\): (\S+)", self.seed()).group(1)
        admin = User.objects.get(email="demo@example.com")
        self.assertTrue(admin.is_app_admin)
        self.assertTrue(admin.check_password(password))
        self.assertTrue(User.objects.get(email="member@example.com").check_password(password))

        personal = services.personal_collection(admin)
        entries = [services.serialize_entry(e, admin) for e in Entry.objects.filter(collection=personal)]
        self.assertGreaterEqual(sum(e["interpreted"] for e in entries), 9)
        self.assertTrue({"Coding agents", "Prompting", "Design"} <= {c for e in entries for c in e["cat"]})
        self.assertTrue(any(e["kind"] == "prompt" for e in entries))
        self.assertEqual(len({e["found"] for e in entries}), len(entries))

        team = Collection.objects.get(name="Team picks")
        self.assertEqual(max(e.contributions.count() for e in team.entries.all()), 2)
        self.assertEqual(set(Job.objects.values_list("state", flat=True)), set(Job.State.values))
        self.assertTrue(Job.objects.filter(state="blocked", error__startswith="interpreter-directed").exists())

    def test_refuses_when_users_exist(self):
        User.objects.create_user(username="real", email="real@example.com")
        with self.assertRaisesMessage(CommandError, "already has users"):
            self.seed()
        self.assertEqual(Entry.objects.count(), 0)

    @override_settings(DEBUG=False)
    def test_refuses_without_debug(self):
        with self.assertRaisesMessage(CommandError, "dev-only"):
            self.seed()
        self.assertFalse(User.objects.exists())
