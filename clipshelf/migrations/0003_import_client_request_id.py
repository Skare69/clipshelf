from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clipshelf', '0002_serversettings_typesafe_api_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='importrecord',
            name='client_request_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name='importrecord',
            constraint=models.UniqueConstraint(
                fields=('user', 'client_request_id'),
                name='clipshelf_import_user_request_unique',
            ),
        ),
    ]
