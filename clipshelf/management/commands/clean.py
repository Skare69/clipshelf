"""List staging cleanup candidates, or delete exactly those with --execute.

The candidate definitions live in clipshelf.worker: job staging dirs, import
media dirs, and staged upload JSON files. Eligibility is ownership-based:
terminal owners past the retention window only, so live work and paths no row
names are unreachable. The default run lists only; nothing is ever deleted
without the explicit --execute approval, and every deleted path is printed.
"""
import shutil

from django.core.management.base import BaseCommand, CommandError

from clipshelf import worker


def _size(path):
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class Command(BaseCommand):
    help = ("List staging cleanup candidates (terminal owners past the "
            "staging retention window: job staging dirs, import media dirs, "
            "staged upload JSON files), or delete exactly those paths with "
            "--execute. Live and unowned staging paths are never listed.")

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true",
                            help="delete the listed candidates (default: list only)")

    def handle(self, *args, **options):
        # ponytail: one snapshot serves listing and deletion; a job requeued
        # in the milliseconds between could lose its fresh staging dir, which
        # the worker re-acquires on the next attempt anyway.
        candidates = (worker.job_staging_cleanup_candidates()
                      + worker.import_media_cleanup_candidates()
                      + worker.staging_cleanup_candidates())
        if not candidates:
            self.stdout.write("no staging cleanup candidates")
            return
        total = 0
        for path in candidates:
            size = _size(path)
            total += size
            self.stdout.write(f"{size:>12}  {path}")
        self.stdout.write(f"{len(candidates)} candidate(s), {total} bytes in total")
        if not options["execute"]:
            self.stdout.write("dry run: nothing deleted (pass --execute)")
            return
        for path in candidates:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:
                raise CommandError(f"failed to delete {path}: {exc}") from exc
            self.stdout.write(f"deleted  {path}")
        self.stdout.write(self.style.SUCCESS(
            f"deleted {len(candidates)} candidate(s)"))
