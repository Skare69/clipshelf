"""Run the single background worker coordinator."""
from django.core.management.base import BaseCommand, CommandError

from clipshelf import worker


class Command(BaseCommand):
    help = ("Run the clipshelf worker coordinator: claims queued jobs, acquires "
            "sources, publishes assets, interprets, and processes staged imports.")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="drain currently claimable work once, then exit")

    def handle(self, *args, **options):
        try:
            worker.main(once=options["once"], stdout=self.stdout)
        except RuntimeError as exc:  # coordinator lock held elsewhere
            raise CommandError(str(exc))
