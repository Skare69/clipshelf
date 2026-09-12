"""Send a real test email through the configured Django email backend."""
import socket
from smtplib import SMTPException

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = ("Send one real message through the configured SMTP backend to prove "
            "delivery works. Reports failures honestly; never pretends success.")

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="recipient email address")
        parser.add_argument("--from", dest="from_email", default=None)

    def handle(self, *args, **options):
        to = options["to"].strip()
        from_email = options["from_email"] or settings.DEFAULT_FROM_EMAIL
        subject = f"[clipshelf] delivery check {timezone.now():%Y-%m-%d %H:%M:%S %Z}".strip()
        body = ("This is a clipshelf delivery check. Invitation and password-reset "
                "mail use this same path, so receiving it means onboarding mail works.")

        using_smtp = "smtp" in str(getattr(settings, "EMAIL_BACKEND", ""))
        backend_desc = (f"{settings.EMAIL_HOST}:{settings.EMAIL_PORT}"
                        if using_smtp and settings.EMAIL_HOST else
                        f"{getattr(settings, 'EMAIL_BACKEND', 'default')} backend")
        try:
            sent = send_mail(subject, body, from_email, [to], fail_silently=False)
        except (SMTPException, socket.error, ImproperlyConfigured, OSError) as exc:
            raise CommandError(f"delivery to {to} failed via {backend_desc}: {exc}")
        if sent != 1:
            raise CommandError(f"delivery to {to} via {backend_desc} sent {sent} messages")
        self.stdout.write(self.style.SUCCESS(f"sent 1 message to {to} via {backend_desc}"))
