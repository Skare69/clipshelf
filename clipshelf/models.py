"""Core Clipshelf domain models (rev. 3).

Accounts, collections, captures, jobs, entries, contributions, scoped history,
retained assets, server settings, invitations, and import records. All
authorization lives in clipshelf.services; models stay plain persistence.
"""
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext_lazy as _


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

    def save(self, *args, **kwargs):
        if not self._state.adding:
            current = ServerSettings.objects.filter(pk=self.pk).values_list("llm_api_key", flat=True).first()
            if current != self.llm_api_key:
                self.llm_verified_at = None  # key changed: capability check must rerun
        super().save(*args, **kwargs)

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
