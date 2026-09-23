"""Core Clipshelf domain models (rev. 3).

Accounts, collections, captures, jobs, entries, contributions, scoped history,
retained assets, server settings, invitations, and import records. All
authorization lives in clipshelf.services; models stay plain persistence.
"""
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext_lazy as _

# Omitted-value sentinel for ServerSettings.resolve; distinct from an explicit
# empty string, which clears the key.
_UNSET = object()


class User(AbstractUser):
    """Application user. Email is the login identity; username stays for the
    framework. session_version participates in the session auth hash so an
    explicit bump invalidates BOTH browser and allauth mobile sessions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(_("email address"), unique=True)
    is_app_admin = models.BooleanField(
        default=False,
        help_text="Application administrator (restricted role, not a Django superuser).",
    )
    session_version = models.IntegerField(default=1)
    default_collection = models.ForeignKey(
        "clipshelf.Collection",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="default_for_users",
    )

    _session_auth_salt = "clipshelf.models.User.session_auth_hash"

    class Meta(AbstractUser.Meta):
        constraints = [
            models.UniqueConstraint(
                models.functions.Lower("email"),
                name="clipshelf_user_email_ci_unique",
            )
        ]

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        super().save(*args, **kwargs)

    def _session_hash_value(self):
        # Salt covers password AND version: either change invalidates sessions.
        return f"{self.session_version}:{self.password}"

    def get_session_auth_hash(self):
        return salted_hmac(
            self._session_auth_salt, self._session_hash_value(), algorithm="sha256"
        ).hexdigest()

    def get_session_auth_fallback_hashes(self):
        return [
            salted_hmac(
                self._session_auth_salt,
                self._session_hash_value(),
                secret=secret,
                algorithm="sha256",
            ).hexdigest()
            for secret in settings.SECRET_KEY_FALLBACKS
        ]


class Collection(models.Model):
    class Kind(models.TextChoices):
        PERSONAL = "personal", "Personal"
        SHARED = "shared", "Shared"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.SHARED)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_collections"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner"],
                condition=Q(kind="personal"),
                name="clipshelf_collection_personal_owner_unique",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.kind})"


class Membership(models.Model):
    """Explicit membership of a shared collection. Owners are implicit members."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="memberships")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "user"], name="clipshelf_membership_unique"
            )
        ]

    def __str__(self):
        return f"{self.user_id} in {self.collection_id}"


class Capture(models.Model):
    """Accepted durable share. Acceptance fields are immutable once stored."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="captures")
    client_request_id = models.UUIDField()
    raw_text = models.TextField()
    requested_collection_id = models.UUIDField(null=True, blank=True)  # what was asked, not an FK
    collection = models.ForeignKey(Collection, on_delete=models.PROTECT, related_name="captures")
    request_hash = models.CharField(max_length=64)
    received_at = models.DateTimeField(auto_now_add=True)
    notice = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "client_request_id"],
                name="clipshelf_capture_user_request_unique",
            )
        ]

    def __str__(self):
        return f"{self.client_request_id} by {self.user_id}"


class Job(models.Model):
    class State(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        RETRY = "retry", "Retry"
        BLOCKED = "blocked", "Blocked"
        DONE = "done", "Done"

    class AcquisitionStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETE = "complete", "Complete"
        PARTIAL = "partial", "Partial"
        BLOCKED = "blocked", "Blocked"
        ERROR = "error", "Error"

    class InterpretationStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETE = "complete", "Complete"
        BLOCKED = "blocked", "Blocked"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capture = models.ForeignKey(Capture, on_delete=models.CASCADE, related_name="jobs")
    collection = models.ForeignKey(Collection, on_delete=models.PROTECT, related_name="jobs")
    url = models.CharField(max_length=2048)
    final_url = models.CharField(max_length=2048, blank=True, default="")
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED)
    acquisition = models.CharField(
        max_length=16, choices=AcquisitionStatus.choices, default=AcquisitionStatus.PENDING
    )
    interpretation = models.CharField(
        max_length=16, choices=InterpretationStatus.choices, default=InterpretationStatus.PENDING
    )
    attempts = models.PositiveIntegerField(default=0)
    retry_at = models.DateTimeField(null=True, blank=True)
    source = models.JSONField(default=dict, blank=True)
    findings = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["capture", "url"], name="clipshelf_job_capture_url_unique")
        ]
        indexes = [models.Index(fields=["state", "retry_at"], name="clipshelf_job_state_retry_idx")]

    @property
    def active(self) -> bool:
        """Queued/running/retry: owned by the scheduler, never retryable."""
        return self.state in {self.State.QUEUED, self.State.RUNNING, self.State.RETRY}

    @property
    def needs_attention(self) -> bool:
        """Blocked as a whole, or any stage the operator must look at."""
        return (
            self.state == self.State.BLOCKED
            or self.acquisition
            in {self.AcquisitionStatus.BLOCKED, self.AcquisitionStatus.ERROR}
            or self.interpretation
            in {self.InterpretationStatus.BLOCKED, self.InterpretationStatus.ERROR}
        )

    GUARDRAIL_PREFIXES = (
        "interpreter-directed content detected",
        "findings withheld:",
    )
    SCREENING_PREFIX = "screening"

    @property
    def guardrail(self) -> bool:
        """Currently blocked by the screening guard rail. State-scoped: a
        retried job requeues with the old error still on the row."""
        return self.state == self.State.BLOCKED and self.error.startswith(
            self.GUARDRAIL_PREFIXES)

    @property
    def screening_warnings(self) -> list:
        """Screening-related warnings, deduplicated the way the payload shows
        all warnings (rows written before the write-side cap can repeat)."""
        return [w for w in dict.fromkeys(self.warnings or [])
                if isinstance(w, str) and w.startswith(self.SCREENING_PREFIX)]

    @property
    def can_retry(self) -> bool:
        """False while queued/running; every other valid state may reprocess."""
        return not self.active

    MAX_ATTEMPTS = 5

    @staticmethod
    def backoff(attempts) -> timedelta:
        return timedelta(seconds=min(30 * 2 ** max(0, attempts - 1), 3600))

    # Lifecycle transitions: the one owner of every state rewrite. Callers own
    # transactions (select_for_update/atomic); these own the field rules and
    # full-row save, because callers legitimately stage other columns
    # (findings, source, warnings) before transitioning.

    def mark_running(self):
        self.state = self.State.RUNNING
        self.retry_at = None
        self.save()

    def defer(self, delay, error):
        """Back to queued with a scheduled retry and an explanatory error."""
        self.state = self.State.QUEUED
        self.retry_at = timezone.now() + delay
        self.error = error
        self.save()

    def mark_blocked(self, error=None, interpretation=None):
        """Terminal scheduled state: no retry is pending, so retry_at clears.
        Deterministic blocks pass interpretation; error=None keeps the row's
        existing message."""
        self.state = self.State.BLOCKED
        self.retry_at = None
        if error is not None:
            self.error = error
        if interpretation is not None:
            self.interpretation = interpretation
        self.save()  # full row: callers may stage findings/source/warnings first

    def mark_done(self, error="", interpretation=None, warnings=None):
        """Terminal success; the findings-withheld variant passes
        interpretation=BLOCKED. warnings=None keeps the row's list."""
        self.state = self.State.DONE
        self.error = error
        self.interpretation = (interpretation if interpretation is not None
                               else self.InterpretationStatus.COMPLETE)
        if warnings is not None:
            self.warnings = warnings
        self.save()  # full row: services stages findings before calling

    def fail(self, exc):
        """One failed attempt: bounded retries with backoff, then blocked."""
        self.attempts = (self.attempts or 0) + 1
        self.error = (str(exc) or exc.__class__.__name__)[:2000]
        if self.attempts >= self.MAX_ATTEMPTS:
            self.state = self.State.BLOCKED
            self.retry_at = None
        else:
            self.state = self.State.RETRY
            self.retry_at = timezone.now() + self.backoff(self.attempts)
        self.save()

    def requeue(self):
        """Operator reprocess; refuses while a run is in flight so the rule
        cannot drift from the mutation."""
        if not self.can_retry:
            raise ValueError("job is already queued or running")
        self.state = self.State.QUEUED
        self.retry_at = None
        self.save()

    def __str__(self):
        return f"{self.url} [{self.state}]"


class Entry(models.Model):
    """One library item per collection: a link (canonical URL) or a prompt
    (normalized digest). Deduplication is collection-scoped only."""

    class Kind(models.TextChoices):
        LINK = "link", "Link"
        PROMPT = "prompt", "Prompt"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="entries")
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.LINK)
    key = models.CharField(max_length=2048)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "kind", "key"], name="clipshelf_entry_unique"
            )
        ]

    def __str__(self):
        return f"{self.kind}:{self.key[:60]}"


class Contribution(models.Model):
    """A member's provenance inside an entry. Metadata lives PER contribution;
    serialization merges only the contributions remaining in the collection."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="contributions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="contributions")
    capture = models.ForeignKey(Capture, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    origin = models.CharField(max_length=32, default="capture")
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entry", "user", "origin"], name="clipshelf_contribution_unique"
            )
        ]

    def __str__(self):
        return f"{self.user_id} -> {self.entry_id}"


class History(models.Model):
    """Collection+user scoped seen/alias/tombstone history. A private user's
    seen row can never suppress another member's contribution."""

    class Kind(models.TextChoices):
        SEEN = "seen", "Seen"
        REMOVED = "removed", "Removed"
        ALIAS = "alias", "Alias"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="history")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="history")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    url = models.CharField(max_length=2048)
    target_url = models.CharField(max_length=2048, blank=True, default="")
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "user", "kind", "url"], name="clipshelf_history_unique"
            )
        ]

    def __str__(self):
        return f"{self.kind}:{self.url[:60]}"


class Asset(models.Model):
    """Retained source file. `collection` is the snapshot authorization at
    store time (never inferred from the job's later fallback)."""

    class Kind(models.TextChoices):
        PAGE = "page", "Page"
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        AUDIO = "audio", "Audio"
        CAPTIONS = "captions", "Captions"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="assets")
    collection = models.ForeignKey(Collection, on_delete=models.PROTECT, related_name="assets")
    path = models.CharField(max_length=1024)  # relative to settings.DATA_DIR, never served raw
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.PAGE)
    content_type = models.CharField(max_length=255, blank=True, default="")
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    position = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.kind}:{self.path}"


class ServerSettings(models.Model):
    """Singleton row (pk=1). llm_api_key is write-only: never serialize it."""

    id = models.AutoField(primary_key=True)
    instance_id = models.UUIDField(default=uuid.uuid4, editable=False)
    llm_base_url = models.CharField(max_length=2048, blank=True, default="")
    llm_model = models.CharField(max_length=200, blank=True, default="")
    llm_api_key = models.CharField(max_length=2048, blank=True, default="")
    llm_concurrency = models.PositiveSmallIntegerField(
        default=2, validators=[MinValueValidator(1), MaxValueValidator(8)]
    )
    llm_verified_at = models.DateTimeField(null=True, blank=True)
    typesafe_api_key = models.CharField(max_length=2048, blank=True, default="")

    def save(self, *args, **kwargs):
        """Verification invalidation and endpoint key isolation live here, so
        every writer (admin API or direct model save) gets the same policy."""
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            tracked = ("llm_base_url", "llm_model", "llm_api_key")
            fields = (
                tracked
                if update_fields is None
                else tuple(f for f in tracked if f in update_fields)
            )
            if fields:
                saved = dict(zip(
                    tracked,
                    ServerSettings.objects.filter(pk=self.pk)
                    .values_list(*tracked)
                    .first(),
                ))
                moved = "llm_base_url" in fields and not self.same_endpoint(
                    saved["llm_base_url"], self.llm_base_url
                )
                changed = moved or any(
                    saved[f] != getattr(self, f) for f in fields if f != "llm_base_url"
                )
                if changed:
                    self.llm_verified_at = None  # actual change: capability check must rerun
                stale_key = moved and self.llm_api_key == saved.get("llm_api_key")
                if stale_key:
                    self.llm_api_key = ""  # a stored key belongs to its old endpoint
                if changed and update_fields is not None:
                    touched = set(update_fields)
                    touched.add("llm_verified_at")
                    if stale_key:
                        touched.add("llm_api_key")
                    kwargs["update_fields"] = touched
        super().save(*args, **kwargs)

    @staticmethod
    def same_endpoint(a: str, b: str) -> bool:
        """Trailing slashes never make a different endpoint."""
        return (a or "").rstrip("/") == (b or "").rstrip("/")

    def resolve(self, *, base_url=None, model=None, api_key=_UNSET):
        """Effective (base_url, model, api_key) shared by save/check/list.

        Omitted values fall back to the saved ones; a stored key is reused only
        for the same endpoint (trailing-slash normalized); an explicit empty
        key clears it. The returned api_key is None when no credentials apply.
        Pure read: preview endpoints consume it without persisting anything.
        """
        base_url = (base_url if base_url is not None else self.llm_base_url or "").strip()
        model = (model if model is not None else self.llm_model or "").strip()
        if api_key is _UNSET:
            api_key = self.llm_api_key if self.same_endpoint(base_url, self.llm_base_url) else ""
        return base_url, model, (api_key or "") or None

    def llm_config(self):
        """Trusted server-side config for worker/interpretation calls only.
        NEVER return this from an API response."""
        return {
            "base_url": self.llm_base_url,
            "model": self.llm_model,
            "api_key": self.llm_api_key,
        }

    def __str__(self):
        return f"ServerSettings {self.instance_id}"


class Invitation(models.Model):
    """Administrator-issued signup invitation. Only the token hash is stored."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    token_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="invitations_created")
    expires_at = models.DateTimeField()
    used_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="invitations_used")
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Invite {self.email}"


class ImportRecord(models.Model):
    """Idempotency ledger for browser tiktok.json imports: one digest per user."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="import_records")
    input_digest = models.CharField(max_length=64)
    manifest = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "input_digest"], name="clipshelf_import_digest_unique"
            )
        ]

    def __str__(self):
        return f"Import {self.input_digest[:12]} by {self.user_id}"
