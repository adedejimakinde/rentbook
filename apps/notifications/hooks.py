"""Queue customer and owner emails. Each runs as a Celery task after the caller commits."""

from apps.core.queue import enqueue_on_commit


def _after_commit(kind, *args):
    from apps.notifications.tasks import send_email

    enqueue_on_commit(send_email, kind, *args)


def booking_received(booking_id):
    _after_commit("booking_received", booking_id)


def booking_confirmed(receipt_id):
    _after_commit("booking_confirmed", receipt_id)


def payment_receipt(receipt_id):
    _after_commit("payment_receipt", receipt_id)


def refund_receipt(receipt_id):
    _after_commit("refund_receipt", receipt_id)


def ach_processing(payment):
    _after_commit("ach_processing", payment.pk)


def owner_new_booking(booking_id):
    _after_commit("owner_new_booking", booking_id)


def owner_payment(receipt_id):
    _after_commit("owner_payment", receipt_id)
