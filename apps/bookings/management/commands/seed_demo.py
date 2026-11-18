"""Fill a development database with demo prices, zones, packages, bookings and logins."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.bookings.services.demo import seed_demo


class Command(BaseCommand):
    help = "Seed demo data for showing the site (development only, needs DEBUG on). Idempotent."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo only runs with DEBUG on. It never touches production.")
        result = seed_demo()
        self.stdout.write(
            self.style.SUCCESS(
                f"Priced {result['variants']} variants, {result['zones']} zones and "
                f"{result['packages']} packages in place; created {result['bookings']} new "
                f"bookings and {result['users']} new users."
            )
        )
