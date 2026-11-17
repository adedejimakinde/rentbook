"""Queueing Celery jobs once the current transaction commits."""

from django.db import transaction


def enqueue(task, *args):
    """Send a Celery task to the broker. Tests replace this to run or record it instead."""
    task.delay(*args)


def enqueue_on_commit(task, *args):
    """Queue task(*args) after the current transaction commits; never if it rolls back.

    A broker failure is logged rather than raised, since the changes the job
    follows from are already committed.
    """
    transaction.on_commit(lambda: enqueue(task, *args), robust=True)
