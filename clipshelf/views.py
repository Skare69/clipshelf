"""Public HTTP surface: authenticated JSON API over core services.

Every /api response is Cache-Control: no-store and authorization goes through
clipshelf.services (collection membership) — no direct global serialization.
Identity comes only from clipshelf.accounts.api_user (framework session or
maintained allauth session token); proxy/forwarded headers are never read.
"""

import hashlib
import json
import os
import re
import uuid as uuid_mod
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.http.request import (
    MultiPartParserError,
    RequestDataTooBig,
)
from django.db import IntegrityError, connection, transaction
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.static import serve as _static_serve

from clipshelf.accounts import ApiError, json_error
from clipshelf import accounts, asset_files, services
from clipshelf.models import (
    Asset,
    Capture,
    Collection,
    Contribution,
    Entry,
    ImportRecord,
    ImportRequest,
    Job,
    Membership,
    User,
)

MAX_BODY_BYTES = 64 * 1024
DEFAULT_PAGE = 50
MAX_PAGE = 500
MAX_NAME_LEN = 200
MAX_IMPORT_DEPTH = 12
STAGING_SUBDIR = "staging"
IMPORT_MAX_BYTES_DEFAULT = 32 * 1024 * 1024
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Browser export must carry media/data only — never session material or tool options.
_BANNED_IMPORT_KEYS = {
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "sessionid",
    "options",
    "path",
}
_INERT_TYPES = {"text/html", "application/xhtml+xml", "application/xml", "text/xml"}


def _json(payload, status=200):
    return JsonResponse(payload, status=status)


def _vmessage(exc):
    msg = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
    if isinstance(msg, dict):
        return "; ".join(f"{k}: {', '.join(v)}" for k, v in msg.items())
    return "; ".join(str(m) for m in msg)




def api(view):
    # accounts.api_csrf is the central conditional-CSRF entry: browser
    # mutations re-run the real CSRF check; requests presenting a session
    # token are exempt only because api_user still has to validate that token
    # (an invalid header never falls back to the cookie session).
    protected = accounts.api_csrf(view)

    @csrf_exempt
    @wraps(protected)
    def wrapper(request, *args, **kwargs):
        try:
            user = accounts.api_user(request)
        except PermissionDenied:
            return json_error(401, detail="unauthenticated")
        request.user = user
        try:
            response = protected(request, *args, **kwargs)
        except ApiError as exc:
            response = exc.response
        except ValidationError as exc:
            conflict = getattr(exc, "code", None) == "conflict"
            response = json_error(
                409 if conflict else 400,
                detail=_vmessage(exc),
            )
        except PermissionDenied:
            response = json_error(403, detail="forbidden")
        except Http404:
            response = json_error(404, detail="no such resource")
        except MultiPartParserError:
            response = json_error(400, detail="malformed multipart body")
        except RequestDataTooBig:
            response = json_error(413, detail="request body too large")
        response["Cache-Control"] = "no-store"
        return response

    return wrapper


def _json_body(request):
    body = request.body or b""
    if len(body) > MAX_BODY_BYTES:
        raise ApiError(413, detail="request body too large")
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        raise ApiError(400, detail="malformed JSON body")
    if not isinstance(data, dict):
        raise ApiError(400, detail="JSON object expected")
    return data


def _uuid_or_400(value, field):
    if not value:
        raise ApiError(400, detail=f"{field} required")
    try:
        return uuid_mod.UUID(str(value))
    except (ValueError, AttributeError):
        raise ApiError(400, detail=f"{field} must be a UUID")


def _import_max_bytes():
    return getattr(settings, "CLIPSHELF_MAX_IMPORT_BYTES", IMPORT_MAX_BYTES_DEFAULT)


def _data_root():
    return os.path.realpath(getattr(settings, "DATA_DIR", ""))


def _accessible_map(user):
    return {str(c.id): c for c in services.accessible_collections(user)}


def _accessible_collection(user, collection_id):
    col = _accessible_map(user).get(str(collection_id))
    if col is None:
        raise Http404("collection not accessible")
    return col


def _collection_json(collection, user):
    return {
        "id": str(collection.id),
        "name": collection.name,
        "kind": collection.kind,
        "owner_id": str(collection.owner_id),
        "can_manage": collection.owner_id == user.id,
    }


def _me_payload(user):
    return {
        "instance_id": str(services.get_settings().instance_id),
        "user": {
            "id": str(user.id),
            "email": user.email,
            "is_app_admin": bool(user.is_app_admin),
        },
        "default_collection_id": (
            str(services.destination_for(user, None)[0].id)
            if user.default_collection_id
            else None
        ),
        "collections": [
            _collection_json(c, user) for c in services.accessible_collections(user)
        ],
    }


def _job_json(job, user=None):
    return {
        "id": str(job.id),
        "url": job.url,
        "final_url": job.final_url,
        "state": job.state,
        "acquisition": job.acquisition,
        "interpretation": job.interpretation,
        # Rows written before the write-side cap can hold repeats; the reader
        # shows each distinct warning once.
        "warnings": list(dict.fromkeys(job.warnings or [])),
        "error": job.error,
        "attempts": job.attempts,
        # Authoritative policy booleans (models.Job): the browser renders
        # activity/error/retry decisions from these instead of re-deriving
        # state rules; colors and labels stay presentation.
        "active": job.active,
        "needs_attention": job.needs_attention,
        "can_retry": job.can_retry and (
            user is None or job.capture.user_id == user.id
        ),
        "guardrail": job.guardrail,
        "screening_warnings": job.screening_warnings,
    }


def _capture_json(capture):
    jobs = Job.objects.filter(capture=capture)
    return {
        "id": str(capture.id),
        "receipt": services.receipt(capture),
        "text": capture.raw_text[:500],
        "jobs": [_job_json(j) for j in jobs],
    }


def _safe_name(path):
    name = os.path.basename(str(path).replace("\\", "/"))
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or "asset"
    return cleaned[:120]


# --- shell, health, static -------------------------------------------------


@ensure_csrf_cookie
def shell(request):
    if not request.user.is_authenticated:
        return redirect(reverse("account_login"))
    response = render(request, "clipshelf/index.html")
    response["Cache-Control"] = "no-store"
    return response


def not_found(request, exception=None):
    if request.path.startswith("/api/"):
        return json_error(404, detail="no such resource")
    return HttpResponse("not found", status=404, content_type="text/plain")



def server_error(request):
    if request.path.startswith("/api/"):
        return json_error(500, detail="internal server error")
    return HttpResponse("server error", status=500, content_type="text/plain")


def healthz(request):
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        services.get_settings()
    except Exception:
        return JsonResponse({"ok": False, "database": False}, status=503)
    return JsonResponse({"ok": True, "database": True})


def static_asset(request, path):
    # The app's own static directory is served straight from the package —
    # never DATA_DIR/media; retained source bytes go through the authorized
    # /api/assets endpoint only. No collectstatic step: this is the source of
    # truth in every mode, so dev and production serve identical bytes.
    # no-cache = store but revalidate: upgrades propagate without hard
    # reloads, and unchanged files still 304.
    response = _static_serve(request, path, document_root=_STATIC_DIR)
    response["Cache-Control"] = "no-cache"
    return response


# --- identity, settings, collections ----------------------------------------


@api
@require_GET
def api_me(request):
    return _json(_me_payload(request.user))


@require_GET
def api_instance(request):
    """Unauthenticated, public instance disclosure. The Android client probes
    a candidate endpoint here before it adopts it (Api.adoptCandidate) and
    refuses a candidate that reports a different instance_id. The id is public,
    so a match is not proof of origin: the client never sends its enrolled
    session token to the candidate and instead signs in there afresh with the
    account password, so only that fresh login's token is used there."""
    response = _json({"instance_id": str(services.get_settings().instance_id)})
    response["Cache-Control"] = "no-store"
    return response


@api
@require_http_methods(["GET", "POST"])
def api_collections(request):
    user = request.user
    if request.method == "GET":
        return _json(
            {
                "collections": [
                    _collection_json(c, user)
                    for c in services.accessible_collections(user)
                ]
            }
        )
    body = _json_body(request)
    name = str(body.get("name") or "").strip()
    if not name or len(name) > MAX_NAME_LEN:
        raise ApiError(400, detail="name must be 1-200 characters")
    collection = Collection.objects.create(name=name, kind="shared", owner=user)
    return _json({"collection": _collection_json(collection, user)}, status=201)


@api
@require_GET
def api_collection_detail(request, collection_id):
    user = request.user
    collection = _accessible_collection(user, collection_id)
    owner = collection.owner
    members = [{"id": str(owner.id), "email": owner.email, "is_owner": True}]
    members += [
        {"id": str(m.user_id), "email": m.user.email, "is_owner": False}
        for m in Membership.objects.filter(collection=collection)
        .exclude(user_id=collection.owner_id)
        .select_related("user")
        .order_by("created_at")
    ]
    return _json(
        {"collection": _collection_json(collection, user), "members": members}
    )


@api
@require_http_methods(["POST"])
def api_collection_members(request, collection_id):
    user = request.user
    collection = _accessible_collection(user, collection_id)
    if collection.kind != "shared":
        raise ApiError(403, detail="personal collections have no members")
    if collection.owner_id != user.id:
        raise ApiError(403, detail="only the owner manages members")
    body = _json_body(request)
    email = str(body.get("email") or "").strip()
    target = User.objects.filter(email__iexact=email, is_active=True).first()
    if target is None:
        raise ApiError(400, detail="no active user with that email")
    if target.id == collection.owner_id:
        raise ApiError(409, detail="owner is already a member")
    try:
        with transaction.atomic():  # keep the outer transaction usable on conflict
            Membership.objects.create(collection=collection, user=target)
    except IntegrityError:
        raise ApiError(409, detail="already a member")
    return _json(
        {
            "member": {
                "id": str(target.id),
                "email": target.email,
                "is_owner": False,
            }
        },
        status=201,
    )


@api
@require_http_methods(["DELETE"])
def api_collection_member(request, collection_id, user_id):
    user = request.user
    collection = _accessible_collection(user, collection_id)
    if collection.kind != "shared":
        raise ApiError(403, detail="personal collections have no members")
    if collection.owner_id != user.id:
        raise ApiError(403, detail="only the owner manages members")
    if str(user_id) == str(collection.owner_id):
        raise ApiError(400, detail="ownership transfers, never member removal")
    membership = Membership.objects.filter(collection=collection, user_id=user_id).first()
    if membership is None:
        raise Http404("not a member")
    membership.delete()
    return _json({"removed": True})


@api
@require_http_methods(["POST"])
def api_settings(request):
    user = request.user
    body = _json_body(request)
    collection = _accessible_collection(user, body.get("default_collection_id"))
    if not services.can_write(user, collection):
        raise ApiError(403, detail="destination is not writable")
    user.default_collection = collection
    user.save(update_fields=["default_collection"])
    return _json(_me_payload(user))


# --- library -----------------------------------------------------------------


def _entry_or_404(user, entry_id):
    try:
        entry = Entry.objects.select_related("collection").get(id=entry_id)
    except (Entry.DoesNotExist, ValueError, ValidationError):
        raise Http404("no such entry")
    if not services.accessible_collections(user).filter(id=entry.collection_id).exists():
        raise Http404("no such entry")
    return entry


def _entry_filters(entries, params):
    q = params.get("q", "").strip().lower()
    cat = params.get("cat", "").strip().lower()
    tag = params.get("tag", "").strip().lower()
    for item in entries:
        if q:
            hay = " ".join(
                str(item.get(f) or "") for f in ("title", "desc", "text", "url")
            ).lower()
            if q not in hay:
                continue
        categories = [str(category).lower() for category in (item.get("cat") or [])]
        if cat and cat not in categories and not (cat == "other" and not categories):
            continue
        if tag and tag not in [str(t).lower() for t in (item.get("tags") or [])]:
            continue
        yield item


@api
@require_GET
def api_entries(request):
    user = request.user
    accessible = list(services.accessible_collections(user))
    if not accessible:
        return _json({"entries": [], "count": 0})
    qs = Entry.objects.filter(collection__in=accessible)
    kind = request.GET.get("kind")
    if kind:
        if kind not in ("link", "prompt"):
            raise ApiError(400, detail="kind must be link or prompt")
        qs = qs.filter(kind=kind)
    collection_id = request.GET.get("collection_id")
    if collection_id:
        collection = _accessible_collection(user, collection_id)
        qs = qs.filter(collection=collection)
    # ponytail: serialize-then-filter is O(collection) per request; fine at
    # personal-library scale, move filtering into core services if it grows.
    pairs = [
        (entry, services.serialize_entry(entry, user))
        for entry in qs.select_related("collection")
    ]
    sort = request.GET.get("sort", "new")
    if sort not in ("az", "new"):
        raise ApiError(400, detail="sort must be az or new")
    if sort == "az":
        pairs.sort(key=lambda p: str(p[1].get("title") or p[1].get("url") or "").lower())
    else:
        pairs.sort(key=lambda p: p[0].created_at, reverse=True)
    filtered = list(_entry_filters((item for _, item in pairs), request.GET))
    try:
        offset = max(0, int(request.GET.get("offset", "0")))
        limit = int(request.GET.get("limit", str(DEFAULT_PAGE)))
    except ValueError:
        raise ApiError(400, detail="offset and limit must be integers")
    if limit <= 0:
        raise ApiError(400, detail="limit must be positive")
    limit = min(limit, MAX_PAGE)
    return _json({"entries": filtered[offset : offset + limit], "count": len(filtered)})


@api
@require_http_methods(["GET", "DELETE"])
def api_entry(request, entry_id):
    user = request.user
    entry = _entry_or_404(user, entry_id)
    if request.method == "DELETE":
        services.remove_entry(user, entry)
        return _json({"removed": True})
    job_ids = entry.contributions.exclude(job=None).values_list("job_id", flat=True)
    jobs = list(Job.objects.filter(id__in=list(job_ids)).select_related("capture"))
    assets = Asset.objects.filter(collection=entry.collection, job__in=jobs).order_by(
        "position", "id"
    )
    return _json(
        {
            "entry": services.serialize_entry(entry, user),
            "assets": [
                {
                    "id": str(a.id),
                    "url": f"/api/assets/{a.id}",
                    "kind": a.kind,
                    "content_type": a.content_type,
                    "name": _safe_name(a.path),
                }
                for a in assets
            ],
            "jobs": [_job_json(j, user) for j in jobs],
        }
    )


@api
@require_GET
def api_asset(request, asset_id):
    user = request.user
    try:
        asset = Asset.objects.get(id=asset_id)
    except (Asset.DoesNotExist, ValueError, ValidationError):
        raise Http404("no such asset")
    # Membership plus a remaining contribution in the snapshot collection
    # referencing this job — a hash/path alone is never a credential.
    if not services.accessible_collections(user).filter(id=asset.collection_id).exists():
        raise Http404("no such asset")
    if not Contribution.objects.filter(
        entry__collection_id=asset.collection_id, job_id=asset.job_id
    ).exists():
        raise Http404("no such asset")
    root = _data_root()
    try:
        full = asset_files.resolve_path(asset.path, root)
    except ValidationError:
        # Invalid, escaped or non-relative-safe stored path: indistinguishable
        # from missing, never a 500 or a leak.
        raise Http404("no such asset")
    if not full.is_file():
        raise Http404("no such asset")
    content_type = (asset.content_type or "application/octet-stream").split(";")[0].strip().lower()
    if content_type in _INERT_TYPES:
        # Retained source pages render as inert text, never executable same-origin.
        content_type = "text/plain; charset=utf-8"
    response = FileResponse(open(full, "rb"), content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="{_safe_name(asset.path)}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    return response


# --- captures, inbox, retry ---------------------------------------------------


def _check_identity(body, user):
    instance_id = body.get("instance_id")
    if not instance_id:
        raise ApiError(400, detail="instance_id required")
    if str(instance_id) != str(services.get_settings().instance_id):
        raise ApiError(409, detail="unknown server instance")
    user_id = body.get("user_id")
    if user_id is not None and str(user_id) != str(user.id):
        raise ApiError(409, detail="client identity does not match account")


@api
@require_http_methods(["GET", "POST"])
def api_captures(request):
    user = request.user
    if request.method == "GET":
        captures = Capture.objects.filter(user=user).order_by("-received_at")
        collection_id = request.GET.get("collection_id")
        if collection_id:
            collection = _accessible_collection(user, collection_id)
            captures = captures.filter(collection=collection)
        return _json({"captures": [_capture_json(c) for c in captures]})

    body = _json_body(request)
    request_id = _uuid_or_400(body.get("client_request_id"), "client_request_id")
    _check_identity(body, user)
    collection_id = body.get("collection_id") or None
    try:
        # services.accept_capture is the sole text/URL policy authority.
        capture, created = services.accept_capture(
            user,
            str(request_id),
            body.get("text"),
            collection_id,
            instance_id=str(body.get("instance_id")),
            user_id=str(body.get("user_id") or user.id),
        )
    except ValidationError as exc:
        if getattr(exc, "code", None) == "oversized":
            raise ApiError(413, detail=_vmessage(exc))
        raise
    except PermissionDenied:
        raise ApiError(403, detail="destination not available")
    return _json({"receipt": services.receipt(capture)}, status=201 if created else 200)


@api
@require_GET
def api_capture_detail(request, capture_id):
    user = request.user
    capture = Capture.objects.filter(id=capture_id, user=user).first()
    if capture is None:
        raise Http404("no such capture")
    if not services.accessible_collections(user).filter(id=capture.collection_id).exists():
        raise Http404("no such capture")
    return _json(_capture_json(capture))


@api
@require_http_methods(["POST"])
def api_job_retry(request, job_id):
    user = request.user
    job = (
        Job.objects.select_related("capture", "collection")
        .filter(id=job_id)
        .first()
    )
    if job is None or job.capture.user_id != user.id:
        raise Http404("no such job")
    if not services.accessible_collections(user).filter(id=job.collection_id).exists():
        raise Http404("no such job")
    # Atomic conditional requeue: a job that became queued/running between the
    # eligibility projection and this write is refused, never double-queued.
    with transaction.atomic():
        current = Job.objects.select_for_update().get(pk=job.pk)
        # Requeue only: retained source/findings stay until the worker replaces
        # them; attempts/backoff history is untouched.
        try:
            current.requeue()
        except ValueError:
            raise ApiError(409, detail="job is already queued or running")
    return _json({"job": _job_json(current)})


# --- browser tiktok.json import ------------------------------------------------


def _scan_import_keys(node, depth=0):
    if depth > MAX_IMPORT_DEPTH:
        raise ApiError(400, detail="export nesting exceeds the supported depth")
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in _BANNED_IMPORT_KEYS:
                raise ApiError(400, detail=f"export must not contain {key!r}")
            _scan_import_keys(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _scan_import_keys(value, depth + 1)


def _bind_request_id(user, request_id, record):
    """Durably map an accepted request id to its import record. Rebinding the
    same record is a no-op; a conflict means the id was concurrently accepted
    for a different payload and must 409."""
    try:
        with transaction.atomic():
            ImportRequest.objects.create(user=user, request_id=request_id, record=record)
    except IntegrityError:
        bound = (
            ImportRequest.objects.select_related("record")
            .filter(user=user, request_id=request_id)
            .first()
        )
        if bound is not None and bound.record_id == record.id:
            return
        if bound is not None:
            raise ApiError(409, detail="request id reused with a different payload")
        raise


def _import_replay(record, spooled):
    """Same-digest retry: discard the re-spooled copy and replay the receipt."""
    os.remove(spooled)
    return _json(
        {
            "import": {
                "id": str(record.id),
                "status": (record.manifest or {}).get("status", "pending"),
            }
        }
    )


@api
@require_http_methods(["POST"])
def api_import(request):
    user = request.user
    upload = request.FILES.get("file")
    if upload is None:
        raise ApiError(400, detail="multipart file required")
    max_bytes = _import_max_bytes()
    if upload.size and upload.size > max_bytes:
        raise ApiError(413, detail="import exceeds the configured ceiling")
    body = request.POST
    request_id = _uuid_or_400(body.get("client_request_id"), "client_request_id")
    _check_identity(body, user)
    collection = _accessible_collection(user, body.get("collection_id"))
    if not services.can_write(user, collection):
        raise ApiError(403, detail="destination not writable")

    root = _data_root()
    staging = os.path.join(root, STAGING_SUBDIR)
    os.makedirs(staging, exist_ok=True)
    rel_path = f"{STAGING_SUBDIR}/import-{uuid_mod.uuid4().hex}.json"
    spooled = os.path.join(root, rel_path)
    digest = hashlib.sha256()
    written = 0
    try:
        with open(spooled, "wb") as out:
            for chunk in upload.chunks():
                written += len(chunk)
                if written > max_bytes:
                    raise ApiError(413, detail="import exceeds the configured ceiling")
                digest.update(chunk)
                out.write(chunk)

        # Upload retry identity is the file digest, not the request id alone;
        # every previously accepted request id is durably bound to its record.
        binding = (
            ImportRequest.objects.select_related("record")
            .filter(user=user, request_id=request_id)
            .first()
        )
        if binding is not None and binding.record.input_digest != digest.hexdigest():
            raise ApiError(409, detail="request id reused with a different payload")
        existing = ImportRecord.objects.filter(user=user, input_digest=digest.hexdigest()).first()
        if existing is not None:
            if (existing.manifest or {}).get("collection_id") != str(collection.id):
                raise ApiError(
                    409, detail="file was already imported to a different collection"
                )
            _bind_request_id(user, request_id, existing)
            return _import_replay(existing, spooled)

        with open(spooled, "rb") as fh:
            try:
                data = json.load(fh)
            except ValueError:
                raise ApiError(400, detail="file is not valid JSON")
        if isinstance(data, list):
            if not all(isinstance(item, dict) for item in data):
                raise ApiError(400, detail="tiktok.json must be a list of objects")
            fmt = "tiktok"
        elif isinstance(data, dict) and isinstance(data.get("links"), dict):
            fmt = "legacy"
        else:
            raise ApiError(
                400,
                detail="file must be a tiktok.json list of objects "
                "or a legacy library.json object with a 'links' object",
            )
        _scan_import_keys(data)

        try:
            with transaction.atomic():
                record = ImportRecord.objects.create(
                    user=user,
                    input_digest=digest.hexdigest(),
                    manifest={
                        "client_request_id": str(request_id),
                        "staging": rel_path,
                        "collection_id": str(collection.id),
                        "status": "pending",
                        "format": fmt,
                    },
                )
                # One transaction: a record never exists without its binding.
                ImportRequest.objects.create(user=user, request_id=request_id, record=record)
        except IntegrityError:
            # A concurrent upload won the digest or the request-id uniqueness
            # race; both inserts above rolled back together.
            record = ImportRecord.objects.filter(
                user=user, input_digest=digest.hexdigest()
            ).first()
            if record is None:
                binding = (
                    ImportRequest.objects.select_related("record")
                    .filter(user=user, request_id=request_id)
                    .first()
                )
                if binding is not None and binding.record.input_digest != digest.hexdigest():
                    # Outer handler removes the staged file.
                    raise ApiError(
                        409, detail="request id reused with a different payload"
                    )
                raise
            if (record.manifest or {}).get("collection_id") != str(collection.id):
                raise ApiError(
                    409, detail="file was already imported to a different collection"
                )
            _bind_request_id(user, request_id, record)
            return _import_replay(record, spooled)
    except Exception:
        try:
            os.remove(spooled)
        except OSError:
            pass
        raise
    return _json({"import": {"id": str(record.id), "status": "pending"}}, status=201)

