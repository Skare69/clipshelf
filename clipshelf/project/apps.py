from django.apps import AppConfig


class ClipshelfConfig(AppConfig):
    name = "clipshelf"
    label = "clipshelf"

    def ready(self):
        # Signals run inside the project dir so the app package stays import-light.
        from clipshelf.project import signals  # noqa: F401
