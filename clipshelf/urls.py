"""Root URL wiring: shell, health, JSON API, account/admin routes, static."""

from django.urls import include, path, re_path

from clipshelf import accounts, setup, views
from clipshelf.admin_views import admin_urlpatterns

urlpatterns = [
    path("", views.shell, name="shell"),
    path("healthz", views.healthz, name="healthz"),
    path("setup", setup.setup_view, name="clipshelf_setup"),
    path("api/me", views.api_me),
    path("api/collections", views.api_collections),
    path("api/collections/<uuid:collection_id>", views.api_collection_detail),
    path(
        "api/collections/<uuid:collection_id>/members",
        views.api_collection_members,
    ),
    path(
        "api/collections/<uuid:collection_id>/members/<uuid:user_id>",
        views.api_collection_member,
    ),
    path("api/settings", views.api_settings),
    path("api/entries", views.api_entries),
    path("api/entries/<uuid:entry_id>", views.api_entry),
    path("api/assets/<uuid:asset_id>", views.api_asset),
    path("", include(accounts.account_urlpatterns)),
    path("api/", include((admin_urlpatterns, "clipshelf_accounts_admin"))),
    path("api/captures", views.api_captures),
    path("api/captures/<uuid:capture_id>", views.api_capture_detail),
    path("api/jobs/<uuid:job_id>/retry", views.api_job_retry),
    path("api/import", views.api_import),
    re_path(r"^static/(?P<path>.*)$", views.static_asset, name="static"),
]

handler404 = views.not_found
handler500 = views.server_error
