"""One template context processor: the deployed version for the page footer."""
import os

RELEASES = "https://github.com/Skare69/clipshelf/releases"


def version(request):
    v = os.environ.get("CLIPSHELF_VERSION") or "dev"
    # a dev build has no tag, so it links to the release list instead
    url = RELEASES if v == "dev" else f"{RELEASES}/tag/v{v}"
    return {"CLIPSHELF_VERSION": v, "CLIPSHELF_RELEASE_URL": url}
