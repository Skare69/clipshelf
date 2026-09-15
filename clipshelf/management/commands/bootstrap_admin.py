"""Operator-only bootstrap of the first application administrator."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import EmailValidator
from django.core.exceptions import ValidationError as DjangoValidationError

from allauth.account.models import EmailAddress

from clipshelf import accounts


class Command(BaseCommand):
    help = ("Create or promote the explicit app administrator for an email "
            "address. The first administrator is normally created by the web "
            "setup wizard at /setup; this command is the recovery path when "
            "that is unavailable. The dev verified-email switch is allowed "
            "only when DEBUG.")

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)

    def handle(self, *args, **options):
        email = options["email"].strip()
        try:
            EmailValidator()(email)
        except DjangoValidationError as exc:
            raise CommandError(f"invalid email: {exc}")
        user, created = accounts.create_admin(email, verified=settings.DEBUG)
        verified = EmailAddress.objects.filter(user=user, verified=True).exists()
        if not verified and not settings.DEBUG:
            # No SMTP is required for the very first administrator: the operator
            # runs this command on the server, so hand them a single-use
            # framework link that verifies the mailbox and sets the password.
            from allauth.account import app_settings as account_settings
            from allauth.account.adapter import get_adapter
            from allauth.account.utils import user_pk_to_url_str

            key = (f"{user_pk_to_url_str(user)}-"
                   f"{account_settings.PASSWORD_RESET_TOKEN_GENERATOR().make_token(user)}")
            setup_url = get_adapter().get_reset_password_from_key_url(key)
            self.stdout.write(self.style.WARNING(
                f"{email} is not verified yet. Open this single-use link to set the "
                f"password, then confirm the address from the account page:\n{setup_url}"))
        self.stdout.write(self.style.SUCCESS(
            f"admin ready: {email} (id={user.id}, created={created}, "
            f"verified={verified}, password={'set' if user.has_usable_password() and not created else 'unusable'})"))
