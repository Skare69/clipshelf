"""Report a user's pending jobs and staged imports."""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F

from clipshelf import models


class Command(BaseCommand):
    help = "List queued/retrying/blocked jobs (and import records) for one user."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True)
        parser.add_argument("--collection", default=None, help="restrict to collection UUID")

    def handle(self, *args, **options):
        user = models.User.objects.filter(email__iexact=options["user"].strip()).first()
        if user is None:
            raise CommandError(f"no account for {options['user']}")
        jobs = models.Job.objects.filter(capture__user=user)
        if options["collection"]:
            jobs = jobs.filter(collection_id=options["collection"])
        jobs = jobs.exclude(state="done").order_by(F("retry_at").asc(nulls_first=True), "id")
        for job in jobs:
            self.stdout.write(
                f"{str(job.id)[:8]} {job.state:8} acq={job.acquisition:9} "
                f"interp={job.interpretation:9} tries={job.attempts} "
                f"retry={job.retry_at.isoformat() if job.retry_at else '-':20} {job.url}")
            if job.error:
                self.stdout.write(f"         error: {job.error[:200]}")
        imports = (models.ImportRecord.objects.filter(user=user)
                   .exclude(manifest__status="done")
                   .order_by("-created_at"))
        for record in imports:
            self.stdout.write(
                f"import  {str(record.id)[:8]} status={record.manifest.get('status', '?'):8} "
                f"{record.manifest.get('error', '')}")
        total = jobs.count()
        self.stdout.write(self.style.SUCCESS(
            f"{total} pending job(s), {imports.count()} open import(s) for {user.email}"))
