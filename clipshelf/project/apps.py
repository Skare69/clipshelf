from django.apps import AppConfig
from django.conf import settings
from django.core import checks


def mail_origin_check(app_configs, **kwargs):
    if settings.EMAIL_BACKEND.endswith(".smtp.EmailBackend") and not settings.CLIPSHELF_ORIGIN:
        return [checks.Warning(
            "SMTP is configured but CLIPSHELF_ORIGIN is not: account mail carries "
            "path-only links that recipients cannot click.",
            hint="Set CLIPSHELF_ORIGIN to the URL users open, e.g. http://nas.example:8000.",
            id="clipshelf.W001",
        )]
    return []


class ClipshelfConfig(AppConfig):
    name = "clipshelf"
    label = "clipshelf"

    def ready(self):
        # Signals run inside the project dir so the app package stays import-light.
        from clipshelf.project import signals  # noqa: F401
        checks.register(mail_origin_check)
