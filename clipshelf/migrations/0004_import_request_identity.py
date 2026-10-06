"""Import request identity: durable per-user binding of every accepted
request id to its ImportRecord.

Replaces the nullable ImportRecord.client_request_id column, which could only
hold one id per record and missed same-digest 200 retries. Legacy bindings
are recovered from the retired column and the manifest copy; the earliest
record wins if a hand-edited manifest collides.
"""

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def bind_legacy_ids(apps, schema_editor):
    ImportRequest = apps.get_model("clipshelf", "ImportRequest")
    ImportRecord = apps.get_model("clipshelf", "ImportRecord")
    for record in ImportRecord.objects.order_by("created_at", "id").iterator():
        ids = set()
        if record.client_request_id:
            ids.add(record.client_request_id)
        raw = (record.manifest or {}).get("client_request_id")
        if raw is not None:
            try:
                ids.add(uuid.UUID(str(raw)))
            except (ValueError, TypeError, AttributeError):
                pass  # legacy manifest junk was never an accepted id
        for request_id in ids:
            ImportRequest.objects.get_or_create(
                user_id=record.user_id,
                request_id=request_id,
                defaults={"record_id": record.pk},
            )


class Migration(migrations.Migration):

    dependencies = [
        ('clipshelf', '0003_import_client_request_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='ImportRequest',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('request_id', models.UUIDField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                (
                    'record',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='request_bindings',
                        to='clipshelf.importrecord',
                    ),
                ),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='import_requests',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name='importrequest',
            constraint=models.UniqueConstraint(
                fields=('user', 'request_id'),
                name='clipshelf_import_request_unique',
            ),
        ),
        # Irreversible: reverse would silently drop durable request-id
        # bindings with no way to reconstruct them.
        migrations.RunPython(bind_legacy_ids),
        migrations.RemoveConstraint(
            model_name='importrecord',
            name='clipshelf_import_user_request_unique',
        ),
        migrations.RemoveField(
            model_name='importrecord',
            name='client_request_id',
        ),
    ]
