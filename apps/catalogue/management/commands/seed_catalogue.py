"""Create the starting catalogue. Safe to run repeatedly."""

from django.core.management.base import BaseCommand

from apps.catalogue.services.seed import seed_catalogue


class Command(BaseCommand):
    help = "Seed categories, items and variants (prices 0, stock unconfirmed). Idempotent."

    def handle(self, *args, **options):
        created = seed_catalogue()
        self.stdout.write(
            self.style.SUCCESS(
                f"Created {created['categories']} categories, {created['items']} items "
                f"and {created['variants']} variants."
            )
        )
