"""One template context processor: the deployed version for the page footer."""
import os


def version(request):
    return {"CLIPSHELF_VERSION": os.environ.get("CLIPSHELF_VERSION") or "dev"}
