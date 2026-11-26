import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clipshelf', '0008_job_url_failures'),
    ]

    operations = [
        migrations.AddField(
            model_name='job',
            name='prompt_tokens',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='job',
            name='completion_tokens',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='serversettings',
            name='llm_output_tokens',
            field=models.PositiveIntegerField(default=4000, validators=[django.core.validators.MinValueValidator(500), django.core.validators.MaxValueValidator(8000)]),
        ),
    ]
