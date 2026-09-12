"""Initial Clipshelf schema (hand-written; mirrors clipshelf.models exactly).

Custom User comes first (settings.AUTH_USER_MODEL), then the collection,
capture/job, entry/contribution, scoped history, asset, settings, invitation,
and import-record tables. Constraints/indexes are added with AddConstraint/
AddIndex (Django 5.2 CreateModel does not take them). Run with
`python clipshelf.py migrate` — never regenerate with makemigrations.
"""
import uuid

import django.contrib.auth.models
import django.contrib.auth.validators
import django.core.validators
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="User",
            fields=[
                ("password", models.CharField(max_length=128, verbose_name="password")),
                ("last_login", models.DateTimeField(blank=True, null=True, verbose_name="last login")),
                (
                    "is_superuser",
                    models.BooleanField(
                        default=False,
                        help_text="Designates that this user has all permissions without explicitly assigning them.",
                        verbose_name="superuser status",
                    ),
                ),
                (
                    "username",
                    models.CharField(
                        error_messages={"unique": "A user with that username already exists."},
                        help_text="Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only.",
                        max_length=150,
                        unique=True,
                        validators=[django.contrib.auth.validators.UnicodeUsernameValidator()],
                        verbose_name="username",
                    ),
                ),
                ("first_name", models.CharField(blank=True, max_length=150, verbose_name="first name")),
                ("last_name", models.CharField(blank=True, max_length=150, verbose_name="last name")),
                (
                    "is_staff",
                    models.BooleanField(
                        default=False,
                        help_text="Designates whether the user can log into this admin site.",
                        verbose_name="staff status",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="Designates whether this user should be treated as active. Unselect this instead of deleting accounts.",
                        verbose_name="active",
                    ),
                ),
                ("date_joined", models.DateTimeField(default=django.utils.timezone.now, verbose_name="date joined")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("email", models.EmailField(max_length=254, unique=True, verbose_name="email address")),
                (
                    "is_app_admin",
                    models.BooleanField(
                        default=False,
                        help_text="Application administrator (restricted role, not a Django superuser).",
                    ),
                ),
                ("session_version", models.IntegerField(default=1)),
                (
                    "groups",
                    models.ManyToManyField(
                        blank=True,
                        help_text="The groups this user belongs to. A user will get all permissions granted to each of their groups.",
                        related_name="user_set",
                        related_query_name="user",
                        to="auth.group",
                        verbose_name="groups",
                    ),
                ),
                (
                    "user_permissions",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Specific permissions for this user.",
                        related_name="user_set",
                        related_query_name="user",
                        to="auth.permission",
                        verbose_name="user permissions",
                    ),
                ),
            ],
            options={
                "verbose_name": "user",
                "verbose_name_plural": "users",
                "abstract": False,
            },
            managers=[
                ("objects", django.contrib.auth.models.UserManager()),
            ],
        ),
        migrations.CreateModel(
            name="Collection",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200)),
                (
                    "kind",
                    models.CharField(
                        choices=[("personal", "Personal"), ("shared", "Shared")],
                        default="shared",
                        max_length=16,
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="owned_collections",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="Membership",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "collection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to="clipshelf.collection",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="memberships",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="Capture",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("client_request_id", models.UUIDField()),
                ("raw_text", models.TextField()),
                ("requested_collection_id", models.UUIDField(blank=True, null=True)),
                ("request_hash", models.CharField(max_length=64)),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("notice", models.TextField(blank=True, default="")),
                (
                    "collection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="captures",
                        to="clipshelf.collection",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="captures",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Job",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("url", models.CharField(max_length=2048)),
                ("final_url", models.CharField(blank=True, default="", max_length=2048)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("retry", "Retry"),
                            ("blocked", "Blocked"),
                            ("done", "Done"),
                        ],
                        default="queued",
                        max_length=16,
                    ),
                ),
                (
                    "acquisition",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("complete", "Complete"),
                            ("partial", "Partial"),
                            ("blocked", "Blocked"),
                            ("error", "Error"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                (
                    "interpretation",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("complete", "Complete"),
                            ("blocked", "Blocked"),
                            ("error", "Error"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("retry_at", models.DateTimeField(blank=True, null=True)),
                ("source", models.JSONField(blank=True, default=dict)),
                ("findings", models.JSONField(blank=True, default=dict)),
                ("warnings", models.JSONField(blank=True, default=list)),
                ("error", models.TextField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "capture",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="jobs",
                        to="clipshelf.capture",
                    ),
                ),
                (
                    "collection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="jobs",
                        to="clipshelf.collection",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Entry",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[("link", "Link"), ("prompt", "Prompt")],
                        default="link",
                        max_length=16,
                    ),
                ),
                ("key", models.CharField(max_length=2048)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "collection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="entries",
                        to="clipshelf.collection",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Contribution",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("origin", models.CharField(default="capture", max_length=32)),
                ("data", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "capture",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="clipshelf.capture",
                    ),
                ),
                (
                    "entry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="contributions",
                        to="clipshelf.entry",
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="clipshelf.job",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="contributions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="History",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[("seen", "Seen"), ("removed", "Removed"), ("alias", "Alias")],
                        max_length=16,
                    ),
                ),
                ("url", models.CharField(max_length=2048)),
                ("target_url", models.CharField(blank=True, default="", max_length=2048)),
                ("data", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "collection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="history",
                        to="clipshelf.collection",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="history",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Asset",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("path", models.CharField(max_length=1024)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("page", "Page"),
                            ("image", "Image"),
                            ("video", "Video"),
                            ("audio", "Audio"),
                            ("captions", "Captions"),
                        ],
                        default="page",
                        max_length=16,
                    ),
                ),
                ("content_type", models.CharField(blank=True, default="", max_length=255)),
                ("size", models.PositiveBigIntegerField(default=0)),
                ("sha256", models.CharField(blank=True, default="", max_length=64)),
                ("position", models.PositiveIntegerField(default=0)),
                (
                    "collection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="assets",
                        to="clipshelf.collection",
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="assets",
                        to="clipshelf.job",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ServerSettings",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("instance_id", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("llm_base_url", models.CharField(blank=True, default="", max_length=2048)),
                ("llm_model", models.CharField(blank=True, default="", max_length=200)),
                ("llm_api_key", models.CharField(blank=True, default="", max_length=2048)),
                (
                    "llm_concurrency",
                    models.PositiveSmallIntegerField(
                        default=2,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(8),
                        ],
                    ),
                ),
                ("llm_verified_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="Invitation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("email", models.EmailField(max_length=254)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invitations_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "used_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="invitations_used",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ImportRecord",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("input_digest", models.CharField(max_length=64)),
                ("manifest", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="import_records",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="user",
            name="default_collection",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="default_for_users",
                to="clipshelf.collection",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                models.functions.Lower("email"),
                name="clipshelf_user_email_ci_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="collection",
            constraint=models.UniqueConstraint(
                condition=models.Q(kind="personal"),
                fields=["owner"],
                name="clipshelf_collection_personal_owner_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="membership",
            constraint=models.UniqueConstraint(
                fields=("collection", "user"), name="clipshelf_membership_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="capture",
            constraint=models.UniqueConstraint(
                fields=("user", "client_request_id"),
                name="clipshelf_capture_user_request_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.UniqueConstraint(
                fields=("capture", "url"), name="clipshelf_job_capture_url_unique"
            ),
        ),
        migrations.AddIndex(
            model_name="job",
            index=models.Index(
                fields=["state", "retry_at"], name="clipshelf_job_state_retry_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="entry",
            constraint=models.UniqueConstraint(
                fields=("collection", "kind", "key"), name="clipshelf_entry_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="contribution",
            constraint=models.UniqueConstraint(
                fields=("entry", "user", "origin"), name="clipshelf_contribution_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="history",
            constraint=models.UniqueConstraint(
                fields=("collection", "user", "kind", "url"),
                name="clipshelf_history_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="importrecord",
            constraint=models.UniqueConstraint(
                fields=("user", "input_digest"), name="clipshelf_import_digest_unique"
            ),
        ),
    ]
