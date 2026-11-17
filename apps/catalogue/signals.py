"""Queue image processing when an upload is added or replaced, and clean up on delete."""

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.catalogue.models import Category, ItemImage, Package, PackageItem
from apps.catalogue.services import images
from apps.catalogue.tasks import process_image
from apps.core.services import clear_section_flags


@receiver(pre_save, sender=ItemImage)
@receiver(pre_save, sender=Category)
def strip_metadata_on_upload(sender, instance, **kwargs):
    """Strip EXIF and GPS from a new upload before the original is written to storage."""
    images.strip_upload_metadata(instance)


@receiver(post_save, sender=Package)
@receiver(post_delete, sender=Package)
@receiver(post_save, sender=PackageItem)
@receiver(post_delete, sender=PackageItem)
@receiver(post_save, sender=ItemImage)
@receiver(post_delete, sender=ItemImage)
def refresh_section_flags(sender, **kwargs):
    """Packages or photos changed, so the nav may need to show or hide a section."""
    transaction.on_commit(clear_section_flags)


@receiver(post_save, sender=ItemImage)
@receiver(post_save, sender=Category)
def queue_image_processing(sender, instance, **kwargs):
    """Mark renditions stale and queue the task whenever the uploaded file changes."""
    if instance.image.name == (getattr(instance, "_loaded_image_name", None) or ""):
        return
    instance._loaded_image_name = instance.image.name
    if not instance.image:
        images.delete_renditions(instance)
        sender.objects.filter(pk=instance.pk).update(
            renditions=[], renditions_ready=False, placeholder=""
        )
        return
    sender.objects.filter(pk=instance.pk).update(renditions_ready=False)
    instance.renditions_ready = False
    label = instance._meta.label_lower
    transaction.on_commit(lambda: process_image.delay(label, instance.pk))


@receiver(post_delete, sender=ItemImage)
@receiver(post_delete, sender=Category)
def delete_image_files(sender, instance, **kwargs):
    """Remove the original and its renditions from storage with the row."""
    images.delete_renditions(instance)
    if instance.image:
        instance.image.storage.delete(instance.image.name)
