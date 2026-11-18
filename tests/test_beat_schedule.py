"""Every scheduled job is in the Celery Beat schedule and names a registered task."""

from django.conf import settings

from config.celery import app

EXPECTED = {
    "release-expired-holds": ("bookings.release_expired_holds", 60),
    "retry-unmatched-stripe-events": ("payments.retry_unmatched_stripe_events", 15 * 60),
    "sync-stale-payments": ("payments.sync_stale_payments", 15 * 60),
    "issue-missing-documents": ("invoices.issue_missing_documents", 15 * 60),
    "sync-recent-paid-bookings": ("payments.sync_recent_paid_bookings", 60 * 60),
    "run-reminders": ("notifications.run_reminders", 15 * 60),
    "send-due-notifications": ("notifications.send_due_notifications", 5 * 60),
}


def test_every_existing_and_new_job_is_scheduled():
    schedule = settings.CELERY_BEAT_SCHEDULE
    assert {
        name: (entry["task"], entry["schedule"]) for name, entry in schedule.items()
    } == EXPECTED


def test_every_scheduled_task_is_registered():
    app.loader.import_default_modules()
    for task, _ in EXPECTED.values():
        assert task in app.tasks, task


def test_beat_uses_the_business_time_zone():
    assert settings.CELERY_TIMEZONE == settings.TIME_ZONE
