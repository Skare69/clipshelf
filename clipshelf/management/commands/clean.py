"""List staging cleanup candidates, or delete still-expired ones with --execute.

The candidate definitions live in clipshelf.worker: job staging dirs, import
media dirs, and staged upload JSON files. Eligibility is ownership-based:
terminal owners past the retention window only, so live work and paths no row
names are unreachable. The default run lists only; nothing is ever deleted
without the explicit --execute approval. --execute takes the exclusive data
lock (the controlled write pause; stop web/worker first) and rechecks every
candidate under the lock, deleting only paths that are still candidates.
"""
import shutil

from django.core.management.base import BaseCommand, CommandError

from clipshelf import worker
from clipshelf.management.locks import data_lock


def _size(path):
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class Command(BaseCommand):
    help = ("List staging cleanup candidates (terminal owners past the "
            "staging retention window: job staging dirs, import media dirs, "
            "staged upload JSON files), or delete them with --execute, which "
            "takes the exclusive data lock (stop web/worker first) and "
            "rechecks each candidate under the lock. Live and unowned staging "
            "paths are never listed.")

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true",
                            help="delete the listed candidates (default: list only)")
        parser.add_argument("--wait", type=float, default=30.0,
                            help="seconds to wait for the exclusive data lock")

    def handle(self, *args, **options):
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
        deleted = skipped = 0
        try:
            with data_lock(exclusive=True, timeout=options["wait"]):
                fresh = set(worker.job_staging_cleanup_candidates()
                            + worker.import_media_cleanup_candidates()
                            + worker.staging_cleanup_candidates())
                for path in candidates:
                    if path not in fresh:
                        self.stdout.write(f"skipped  {path} (no longer a candidate)")
                        skipped += 1
                        continue
                    try:
                        if path.is_dir():
                            shutil.rmtree(path)
                        else:
                            path.unlink()
                    except OSError as exc:
                        raise CommandError(f"failed to delete {path}: {exc}") from exc
                    self.stdout.write(f"deleted  {path}")
                    deleted += 1
        except TimeoutError as exc:
            raise CommandError(str(exc))
        if deleted:
            self.stdout.write(self.style.SUCCESS(
                f"deleted {deleted} candidate(s), skipped {skipped}"))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"deleted no candidates, skipped {skipped}"))
