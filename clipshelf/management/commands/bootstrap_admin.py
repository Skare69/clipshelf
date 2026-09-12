"""Operator-only bootstrap of the first application administrator."""
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import EmailValidator
from django.core.exceptions import ValidationError as DjangoValidationError

from clipshelf import models


class Command(BaseCommand):
    help = ("Create or promote the explicit app administrator for an email "
            "address. The dev verified-email switch is allowed only when DEBUG.")

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)

    def handle(self, *args, **options):
        email = options["email"].strip()
        try:
            EmailValidator()(email)
        except DjangoValidationError as exc:
            raise CommandError(f"invalid email: {exc}")
        user = models.User.objects.filter(email__iexact=email).first()
        created = False
        if user is None:
            base = re.sub(r"[^a-z0-9_.-]+", "", email.split("@")[0].lower()) or "clipshelf"
            username, n = base, 1
            while models.User.objects.filter(username__iexact=username).exists():
                username = f"{base}-{n}"
                n += 1
            user = models.User.objects.create_user(username=username, email=email)
            created = True  # unusable password: operator or user sets it via the framework
        user.is_app_admin = True
        user.save(update_fields=["is_app_admin"])

        verified = False
        try:
            from allauth.account.models import EmailAddress
        except ImportError:
            EmailAddress = None
        if EmailAddress is not None:
            address = EmailAddress.objects.filter(user=user).first()
            if address is None:
                address = EmailAddress(user=user, email=email, primary=True, verified=False)
                address.save()
            if settings.DEBUG:
                address.verified = True  # dev-only switch
                address.save()
            verified = address.verified
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
