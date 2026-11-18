"""Forget the cached site settings whenever the row is saved."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.core import site_settings
from apps.core.models import SiteSettings


@receiver(post_save, sender=SiteSettings)
def forget_cached_settings(sender, **kwargs):
    site_settings.forget()
