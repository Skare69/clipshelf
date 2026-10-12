"""Shared helpers for clipshelf management commands."""
from django.core.management.base import CommandError

from clipshelf import models


def user_for(email):
    """Return the account for a --user email, or raise the shared CommandError."""
    user = models.User.objects.filter(email__iexact=email.strip()).first()
    if user is None:
        raise CommandError(f"no account for {email}")
    return user
